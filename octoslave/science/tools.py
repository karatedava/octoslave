"""Science capability tools, registered as dynamic tools while a run is active.

Signature contract (same as the Lab foundry): ``run(args, working_dir) ->
(text, ok)``. Run context (session/client/model/emit) comes from
``science.context``.
"""

from __future__ import annotations

import json
import posixpath
import shlex
import subprocess
import threading
import time
from pathlib import Path

from .. import tools as _tools
from . import context as _ctx
from . import workspace as _workspace
from .session import Artifact, Job, Specialist

# ---------------------------------------------------------------------------
# Compute context for the cluster-job tools (submit / check / fetch)
#
# These three tools are reused by BOTH the Science orchestrator and the
# autonomous Lab. Science stashes its RunContext module-side (see context.py);
# the Lab sets a THREAD-LOCAL context here with a LabSession-compatible adapter.
# The thread-local wins when set, else we fall back to the science context — so
# science behaviour is unchanged, and a Lab run on another thread is isolated.
# The context object just needs ``.session`` (with working_dir, remote_id,
# add_job/get_job/jobs, science_dir, save) and a callable ``.emit``.
# ---------------------------------------------------------------------------
_COMPUTE = threading.local()


def set_compute_context(session, emit) -> None:
    """Point this thread's cluster-job tools at ``session`` (working_dir /
    remote_id / job store) with ``emit`` for progress events."""
    _COMPUTE.ctx = type("_ComputeCtx", (), {"session": session, "emit": emit})()


def clear_compute_context() -> None:
    _COMPUTE.ctx = None


def _compute_ctx():
    """Active cluster-job context: the thread-local one if set (Lab), else the
    science module context (Science)."""
    c = getattr(_COMPUTE, "ctx", None)
    return c if c is not None else _ctx.current()


def _emit_compute(event: dict) -> None:
    ctx = _compute_ctx()
    if ctx and getattr(ctx, "emit", None):
        try:
            ctx.emit(event)
        except Exception:
            pass


def active_compute_node() -> dict | None:
    """The remote (SSH) compute node configured for the active cluster-job
    context, or None. Used to make Lab/Science agents aware of the node."""
    ctx = _compute_ctx()
    rid = getattr(getattr(ctx, "session", None), "remote_id", "") or ""
    return _remote_for(rid) if rid else None

# ---------------------------------------------------------------------------
# Speaker tagging
#
# Specialists run on their OWN threads, several at once, and all of them emit
# through the one channel the orchestrator uses — so without a tag the UI would
# attribute their turns to the orchestrator, and two concurrent specialists to
# each other. Tagging is therefore per-thread: the block below stamps every
# event emitted on THIS thread (the science context callback and display's own
# thread-local one, which carries the token/tool events) with who produced it,
# and touches no other thread's state.
# ---------------------------------------------------------------------------


class _speaker:
    """Context manager: stamp events emitted on this thread with a speaker."""

    def __init__(self, ctx, *, agent_id: str, name: str, role: str, icon: str,
                 on_step=None):
        self._ctx = ctx
        self._tag = {"agent_id": agent_id, "agent_name": name,
                     "agent_role": role, "agent_icon": icon}
        self._on_step = on_step
        self._prev_display = None
        self._prev_tag = None

    def emit(self, event: dict) -> None:
        """Emit one event as this speaker (usable from the specialist thread)."""
        inner = getattr(self._ctx, "emit", None)
        if inner is None:
            return
        if isinstance(event, dict) and "agent_name" not in event:
            event = {**event, **self._tag}
        try:
            inner(event)
        except Exception:
            pass

    def _wrap(self, inner):
        tag, on_step, ctx = self._tag, self._on_step, self._ctx

        def tagged(event: dict) -> None:
            if isinstance(event, dict):
                if "agent_name" not in event:
                    event = {**event, **tag}
                # Progress for the orchestrator's check_specialists view.
                if on_step is not None and event.get("type") == "tool_call":
                    try:
                        on_step(event.get("summary") or event.get("name") or "")
                    except Exception:
                        pass
            # A specialist thread starts with no display callback of its own
            # (it is thread-local), so fall back to the session channel — else
            # everything the specialist streams would be dropped.
            sink = inner if inner is not None else getattr(ctx, "emit", None)
            if sink is None:
                return
            try:
                sink(event)
            except Exception:
                pass

        return tagged

    def __enter__(self):
        self._prev_tag = _ctx.speaker()
        _ctx.set_speaker(self._tag)
        from .. import display
        self._prev_display = display.get_event_callback()
        display.set_event_callback(self._wrap(self._prev_display))
        return self

    def __exit__(self, *exc):
        _ctx.set_speaker(self._prev_tag)
        from .. import display
        display.set_event_callback(self._prev_display)
        return False


# ---------------------------------------------------------------------------
# Artifact kind inference
# ---------------------------------------------------------------------------

_IMG = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_TABLE = {".csv", ".tsv", ".parquet", ".xlsx"}
_REPORT = {".html", ".htm", ".md", ".pdf"}
_TEXT = {".txt", ".json", ".yaml", ".yml", ".log"}


def _infer_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _IMG:
        return "image"
    if ext in _TABLE:
        return "table"
    if ext in _REPORT:
        return "report"
    if ext in _TEXT:
        return "text"
    return "file"


def _relpath(path: Path, working_dir: str) -> str:
    try:
        return str(path.resolve().relative_to(Path(working_dir).resolve()))
    except Exception:
        return path.name


# ---------------------------------------------------------------------------
# spawn_specialist / continue_specialist
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset("""a an and are as at be by for from in into of on or the to with
that this it its their they you your using ensure across target focus other more than
all any new work task goal agent specialist please should must can will only just""".split())


def _terms(text: str) -> set[str]:
    """Content words of a goal/role line, crudely singularised, for comparison."""
    import re
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) <= 2 or w in _STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def _overlap(a: str, b: str) -> float:
    """How much of the SMALLER description is contained in the larger one.

    Containment rather than Jaccard: a re-worded, narrower restatement of an
    existing remit (the usual near-clone) still scores high, while genuinely
    different work in the same field scores low.
    """
    ta, tb = _terms(a), _terms(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# Calibrated on real sessions: a re-worded clone of an existing remit lands
# around 0.4+, while distinct work in the same field (modelling, plotting,
# structure prediction over the same data) stays near 0.1.
_DUP_THRESHOLD = 0.3
_MIN_TERMS = 4


def _duplicate_of(session, role: str, goal: str) -> "Specialist | None":
    """An existing specialist whose remit substantially covers ``goal``.

    Spawning a near-clone is the common failure mode: the fresh agent has none of
    the first one's findings, so it re-treads the same sources and dead ends.
    """
    best, best_score = None, 0.0
    for s in session.specialists:
        if s.status not in ("working", "done"):
            continue
        # Too-short descriptions carry no signal — don't block on them.
        if min(len(_terms(goal)), len(_terms(s.goal))) < _MIN_TERMS:
            continue
        score = max(_overlap(goal, s.goal), _overlap(f"{role} {goal}", f"{s.role} {s.goal}"))
        if score > best_score:
            best, best_score = s, score
    return best if best_score >= _DUP_THRESHOLD else None


def _prior_work(session, exclude_id: str = "") -> str:
    """A digest of what earlier specialists already did, handed to a new one so it
    starts where they left off instead of repeating them."""
    lines = []
    for s in session.specialists:
        if s.id == exclude_id or not (s.summary or "").strip():
            continue
        lines.append(f"- {s.name} ({s.role}) — {s.goal}\n  Outcome: "
                     + " ".join((s.summary or "").split())[:600])
    if not lines:
        return ""
    return ("\n\n## Work already done in this session (do NOT repeat it)\n"
            + "\n".join(lines[-6:]))


_SPECIALIST_BRIEF = ("You are a specialist reporting to a research orchestrator. "
                     "Stay strictly within your goal. A researcher is watching "
                     "live: when a figure or table they would want to see exists — "
                     "even a first pass while your work continues — show it with "
                     "present_output (interim=true for an early look), and note "
                     "its exact path in your final summary.")

# Tools every specialist gets on top of its grant: surfacing its own results,
# recording how they were made, and LOOKING at a figure or page before reporting
# it. The other science tools (dispatching agents, cluster jobs) stay with the
# orchestrator — no recursion, one place that owns compute.
SPECIALIST_SHARED_TOOLS = ("present_output", "record_provenance", "view_image")


def _run_specialist(ctx, rec, spec, task: str, working_dir: str, *,
                    history: list[dict] | None = None,
                    note: str = "") -> tuple[str, bool]:
    """Dispatch (or resume) one specialist ON ITS OWN THREAD and return at once.

    The orchestrator is not blocked: it keeps talking to the researcher, can
    dispatch more specialists, and picks the result up later — handed to it
    automatically when the specialist reports back, or on demand through
    check_specialists. ``note`` is prepended to the reply, used to tell the
    orchestrator when one of its choices (e.g. a model id) was overridden.
    """
    from .. import interrupt
    from . import specialists as _spec

    pool = getattr(ctx, "pool", None)
    if pool is None:
        # No supervisor for this run (a caller that didn't set one up) — fall
        # back to running inline rather than losing the work.
        return _run_specialist_blocking(ctx, rec, spec, task, working_dir,
                                        history=history, note=note)

    ctx.session.update_specialist(rec.id, status="working")
    run = _spec.Run(id=rec.id, name=rec.name, role=rec.role, goal=rec.goal,
                    icon=rec.icon, model=spec.model)
    parent = threading.get_ident()
    inbox = getattr(ctx, "inbox", None)
    if inbox is not None:
        inbox.mark_seen(rec.name)
    _ctx.emit({"type": "science_specialist", "event": "start", "id": rec.id,
               "name": rec.name, "role": rec.role, "goal": rec.goal,
               "tools": rec.tools, "icon": rec.icon, "model": spec.model,
               "resumed": bool(history), "background": True})
    context = _SPECIALIST_BRIEF + ("" if history else _prior_work(ctx.session, rec.id))

    def _work() -> None:
        from .. import steer
        # Follow the orchestrator's stop signal (one Stop from the user ends
        # every specialist too), with a signal of its own so the orchestrator
        # can also end it without stopping itself.
        own = interrupt.adopt(parent)
        # The context is per thread: this specialist works for THIS session.
        _ctx.set_context(ctx)
        if not pool.attach(rec.id):
            own.set()                       # the turn was torn down already
        # Let the specialist see live user messages as an FYI (steer.relay).
        if inbox is not None:
            steer.bind(inbox)
        sp = _speaker(ctx, agent_id=rec.id, name=rec.name, role=rec.role,
                      icon=rec.icon,
                      on_step=lambda label: pool.note_step(rec.id, label))
        try:
            with sp:
                _finish_specialist(ctx, pool, rec, spec, task, working_dir,
                                   history, context, sp)
        except BaseException as exc:  # noqa: BLE001 — must never hang the turn
            pool.finish(rec.id, "failed",
                        f"Specialist '{rec.name}' failed: {exc}", ok=False)
            try:
                ctx.session.update_specialist(rec.id, status="failed",
                                              summary=f"error: {exc}")
            except Exception:
                pass
            sp.emit({"type": "science_specialist", "event": "done", "id": rec.id,
                     "status": "failed", "summary": str(exc)})
        finally:
            # Whatever happened above, the orchestrator must hear that this
            # run is over — a run left "working" would keep the turn waiting.
            pool.finish(rec.id, "failed", f"Specialist '{rec.name}' ended "
                        "without reporting back.", ok=False)
            if inbox is not None:
                steer.unbind()
            _ctx.clear_context()
            interrupt.unregister()

    pool.start(run, _work, ())
    verb = "Resumed" if history else "Dispatched"
    return ((f"{note}\n" if note else "")
            + f"{verb} specialist '{rec.name}' ({rec.role}), id `{rec.id}`, "
            f"running in the background on {spec.model}.\n\n"
            f"You are NOT blocked. Carry on: tell the researcher what you just "
            f"put in motion, dispatch any other independent work, and do what "
            f"you can yourself meanwhile. Its report is handed to you "
            f"automatically the moment it finishes — you do not need to poll for "
            f"it. Use check_specialists only when you want to see progress right "
            f"now, or to wait when you genuinely have nothing else to do.", True)


def _finish_specialist(ctx, pool, rec, spec, task, working_dir, history,
                       context, sp) -> None:
    """The body of a background specialist run (on the specialist's thread)."""
    from ..lab.agent_runtime import run_agent_task
    from .. import interrupt
    try:
        transcript, summary = run_agent_task(
            spec, task, working_dir, ctx.client,
            model=spec.model, permission_mode=ctx.permission_mode,
            context=context, emit=sp.emit, history=history,
            # If this specialist's model dies or its endpoint rejects what it
            # produces, carry on with another model from the configured pool.
            model_pool=list(getattr(ctx, "specialist_models", []) or []) or [ctx.model],
            setting="science",
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, interrupt.StopRequested):
            # The user stopped the session. Keep whatever this specialist had
            # done so it can be resumed, and mark it honestly.
            partial = getattr(exc, "transcript", None)
            if partial:
                ctx.session.save_transcript(rec.id, partial)
            msg = ("⚠ INCOMPLETE — the user stopped the session while this "
                   "specialist was working. Its progress is preserved; resume "
                   "it with continue_specialist.")
            ctx.session.update_specialist(rec.id, status="done", summary=msg)
            sp.emit({"type": "science_specialist", "event": "done", "id": rec.id,
                     "status": "done", "summary": "stopped by the user (resumable)"})
            pool.finish(rec.id, "stopped", msg, ok=False)
            return
        ctx.session.update_specialist(rec.id, status="failed", summary=f"error: {exc}")
        sp.emit({"type": "science_specialist", "event": "done", "id": rec.id,
                 "status": "failed", "summary": str(exc)})
        pool.finish(rec.id, "failed", f"Specialist '{rec.name}' failed: {exc}", ok=False)
        return

    summary = (summary or "").strip() or "(no summary returned)"
    # Keep the transcript so this specialist can be RESUMED (continue_specialist)
    # with everything it learned, instead of being cloned from scratch.
    ctx.session.save_transcript(rec.id, transcript)
    ctx.session.update_specialist(rec.id, status="done", summary=summary)
    sp.emit({"type": "science_specialist", "event": "done", "id": rec.id,
             "status": "done", "summary": summary})
    pool.finish(rec.id, "done", summary, ok=True)


def _run_specialist_blocking(ctx, rec, spec, task: str, working_dir: str, *,
                             history: list[dict] | None = None,
                             note: str = "") -> tuple[str, bool]:
    """Run a specialist inline, blocking until it finishes.

    Used when no supervisor pool is active, and when the orchestrator explicitly
    asks to wait because it cannot do anything until the result is in.
    """
    from ..lab.agent_runtime import run_agent_task
    from .. import interrupt

    ctx.session.update_specialist(rec.id, status="working")
    if getattr(ctx, "inbox", None) is not None:
        ctx.inbox.mark_seen(rec.name)
    _ctx.emit({"type": "science_specialist", "event": "start", "id": rec.id,
               "name": rec.name, "role": rec.role, "goal": rec.goal,
               "tools": rec.tools, "icon": rec.icon, "model": spec.model,
               "resumed": bool(history)})
    context = _SPECIALIST_BRIEF + ("" if history else _prior_work(ctx.session, rec.id))
    try:
        with _speaker(ctx, agent_id=rec.id, name=rec.name, role=rec.role,
                      icon=rec.icon) as sp:
            transcript, summary = run_agent_task(
                spec, task, working_dir, ctx.client,
                model=spec.model, permission_mode=ctx.permission_mode,
                context=context, emit=sp.emit, history=history,
                model_pool=list(getattr(ctx, "specialist_models", []) or []) or [ctx.model],
                setting="science",
            )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, interrupt.StopRequested):
            partial = getattr(exc, "transcript", None)
            if partial:
                ctx.session.save_transcript(rec.id, partial)
            ctx.session.update_specialist(
                rec.id, status="done",
                summary="⚠ INCOMPLETE — the user stopped the session while this "
                        "specialist was working. Its progress is preserved; resume "
                        "it with continue_specialist.")
            _ctx.emit({"type": "science_specialist", "event": "done", "id": rec.id,
                       "status": "done", "summary": "stopped by the user (resumable)"})
            raise
        if not isinstance(exc, Exception):
            raise
        ctx.session.update_specialist(rec.id, status="failed", summary=f"error: {exc}")
        _ctx.emit({"type": "science_specialist", "event": "done", "id": rec.id,
                   "status": "failed", "summary": str(exc)})
        return f"Specialist '{rec.name}' failed: {exc}", False

    summary = (summary or "").strip() or "(no summary returned)"
    ctx.session.save_transcript(rec.id, transcript)
    ctx.session.update_specialist(rec.id, status="done", summary=summary)
    _ctx.emit({"type": "science_specialist", "event": "done", "id": rec.id,
               "status": "done", "summary": summary})
    verb = "picked up where it left off and finished" if history else "finished"
    return ((f"{note}\n" if note else "")
            + f"Specialist '{rec.name}' ({rec.role}) {verb}. Its id is `{rec.id}` — "
            f"use continue_specialist to give it more work rather than spawning "
            f"another agent for the same area.\n\n{summary}", True)


def _resolve_specialist_model(ctx, requested) -> tuple[str, str]:
    """Which model a specialist runs on, and a note if the request was refused.

    The orchestrator picks from the configured pool. It sometimes names a model
    it knows from training rather than one this backend serves (``gpt-4.1`` and
    friends) — running that fails on the first call, so an id that is neither in
    the pool nor in the live catalog is REFUSED here and the note tells the
    orchestrator what it may actually choose from.
    """
    pool = [m for m in (getattr(ctx, "specialist_models", []) or []) if m]
    default = pool[0] if pool else ctx.model
    requested = str(requested or "").strip()
    if not requested or requested == default:
        return default, ""
    if pool:
        if requested in pool:
            return requested, ""
        return default, (f"Note: '{requested}' is not in this session's specialist "
                         f"pool, so it ran on {default}. Choose from: "
                         f"{', '.join(pool)}.")
    # No pool configured — only the live catalog can vouch for the id.
    try:
        from ..config import is_chat_model, list_models, load_config
        catalog = [m for m in (list_models(load_config()) or []) if is_chat_model(m)]
    except Exception:  # noqa: BLE001 — catalog unreachable; trust the request
        return requested, ""
    if not catalog or requested in catalog:
        return requested, ""
    return default, (f"Note: '{requested}' is not available on this backend, so it "
                     f"ran on {default}. Don't pass `model` unless you're picking "
                     f"from the models listed in your system prompt.")


def _run_spawn_specialist(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _ctx.current()
    if ctx is None:
        return "Science context unavailable.", False

    name = str(args.get("name", "")).strip() or "Specialist"
    role = str(args.get("role", "")).strip() or name
    goal = str(args.get("goal", "")).strip()
    task = str(args.get("task", "")).strip() or goal
    icon = str(args.get("icon", "")).strip() or "🔬"
    req_tools = args.get("tools") or []
    if isinstance(req_tools, str):
        req_tools = [t.strip() for t in req_tools.split(",") if t.strip()]
    if not goal and not task:
        return "spawn_specialist needs a `goal` (and ideally a `task`).", False

    valid = _tools.valid_tool_names()
    # Never hand a spawned specialist the meta science tools (no recursion).
    granted = [t for t in req_tools if t in valid and t not in SCIENCE_TOOL_NAMES]
    if not granted:
        granted = ["read_file", "write_file", "edit_file", "bash",
                   "glob", "grep", "list_dir"]
    # Every specialist can surface its own (interim) results to the researcher.
    granted += [t for t in SPECIALIST_SHARED_TOOLS if t in valid and t not in granted]

    spec_model, model_note = _resolve_specialist_model(ctx, args.get("model"))

    # Don't clone an existing specialist. If one already covers this remit, the
    # orchestrator should resume it (it keeps the sources it already tried) —
    # unless it deliberately says this is a different angle.
    if not _truthy(args.get("force")):
        dup = _duplicate_of(ctx.session, role, goal or task)
        if dup is not None:
            return (
                f"Not spawned — '{dup.name}' ({dup.role}, id `{dup.id}`, status "
                f"{dup.status}) already covers this remit:\n  {dup.goal}\n\n"
                f"Resume it with continue_specialist(id=\"{dup.id}\", task=…) so it "
                f"keeps everything it already found and the dead ends it already hit. "
                f"Only spawn a new specialist if this is genuinely different work — "
                f"then say so in the goal (and pass force=true).", False)

    from ..lab.state import AgentSpec

    spec = AgentSpec(name=name, role=role, expertise=role, goal=goal or task,
                     tools=granted, model=spec_model, icon=icon)
    rec = Specialist(name=name, role=role, goal=goal or task, tools=granted,
                     icon=icon, status="working")
    ctx.session.add_specialist(rec)
    runner = _run_specialist_blocking if _truthy(args.get("wait")) else _run_specialist
    return runner(ctx, rec, spec, task or goal, working_dir, note=model_note)


def _run_continue_specialist(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _ctx.current()
    if ctx is None:
        return "Science context unavailable.", False

    ref = str(args.get("id") or args.get("specialist_id") or args.get("name", "")).strip()
    task = str(args.get("task", "")).strip()
    rec = ctx.session.get_specialist(ref)
    if rec is None:
        known = ", ".join(f"{s.name} (`{s.id}`)" for s in ctx.session.specialists) or "none"
        return f"No specialist '{ref}' in this session. Known specialists: {known}.", False
    if not task:
        return "continue_specialist needs a `task` — what should it do next?", False
    if rec.status == "working":
        return f"'{rec.name}' is already working; wait for it to report back.", False

    from ..lab.state import AgentSpec

    history = ctx.session.load_transcript(rec.id)
    tools = list(rec.tools)
    extra = args.get("tools") or []
    if isinstance(extra, str):
        extra = [t.strip() for t in extra.split(",") if t.strip()]
    valid = _tools.valid_tool_names()
    for t in extra:
        if t in valid and t not in SCIENCE_TOOL_NAMES and t not in tools:
            tools.append(t)
    for t in SPECIALIST_SHARED_TOOLS:
        if t in valid and t not in tools:
            tools.append(t)
    if tools != rec.tools:
        ctx.session.update_specialist(rec.id, tools=tools)
        rec.tools = tools

    spec_model, model_note = _resolve_specialist_model(ctx, args.get("model"))
    spec = AgentSpec(name=rec.name, role=rec.role, expertise=rec.role, goal=rec.goal,
                     tools=tools, model=spec_model, icon=rec.icon)
    if not history:
        # Transcript lost (e.g. an older session) — run it fresh, but hand over
        # what it reported last time so it doesn't start from zero.
        task = (f"{task}\n\n(Your previous run reported: "
                f"{' '.join((rec.summary or 'nothing recorded').split())[:800]})")
    runner = _run_specialist_blocking if _truthy(args.get("wait")) else _run_specialist
    return runner(ctx, rec, spec, task, working_dir,
                  history=history or None, note=model_note)


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _run_check_specialists(args: dict, working_dir: str) -> tuple[str, bool]:
    """What the dispatched specialists are doing, and anything they reported."""
    ctx = _ctx.current()
    if ctx is None:
        return "Science context unavailable.", False
    pool = getattr(ctx, "pool", None)
    if pool is None:
        return ("No specialists are running in the background in this turn.", True)

    from . import specialists as _spec
    from .. import interrupt

    if _truthy(args.get("wait")) and pool.any_running():
        try:
            timeout = float(args.get("timeout") or 300)
        except (TypeError, ValueError):
            timeout = 300.0
        timeout = max(5.0, min(1800.0, timeout))
        deadline = time.monotonic() + timeout
        # Wake early on a stop, and on a live user message — the researcher
        # must never be left waiting behind a specialist.
        inbox = getattr(ctx, "inbox", None)
        while time.monotonic() < deadline:
            if pool.wait(min(2.0, max(0.1, deadline - time.monotonic()))):
                break
            if interrupt.should_stop():
                break
            if inbox is not None and inbox.pending():
                break

    done = pool.collect()
    parts = []
    if done:
        parts.append(_spec.format_reports(done, pool))
    parts.append("## Specialists\n" + pool.snapshot())
    if pool.any_running():
        parts.append("They are still working — carry on with something useful "
                     "rather than polling in a loop; their reports reach you "
                     "automatically when they land.")
    return "\n\n".join(parts), True


# ---------------------------------------------------------------------------
# Running a short command ON the compute node
#
# Without this the ONLY way to touch the node is submit_cluster_job, so every
# `ls`, `module avail` or queue check becomes a "job" — the researcher's Jobs
# panel fills with things that are not jobs, and the real computations are lost
# among them. This runs a short command and returns its output like any other
# tool call: no job record, no entry in the Jobs panel.
# ---------------------------------------------------------------------------

_SHELL_BUDGET = 8000


def _run_cluster_shell(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _compute_ctx()
    if ctx is None:
        return "Compute context unavailable.", False
    command = str(args.get("command", "")).strip()
    if not command:
        return "cluster_shell needs a `command`.", False
    remote_id = str(args.get("remote_id", "")).strip() or \
        getattr(ctx.session, "remote_id", "") or ""
    remote = _remote_for(remote_id)
    if not remote:
        return ("cluster_shell needs a compute node, but this session has none "
                "configured — run the command locally with bash instead.", False)
    try:
        timeout = int(args.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(5, min(600, timeout))
    cwd = _remote_job_cwd(remote, ctx.session, str(args.get("cwd", "")).strip())

    from ..remote import RemoteSession
    try:
        out, err, code = RemoteSession.get(remote).run(command, cwd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return f"cluster_shell failed on {remote.get('name', remote_id)}: {exc}", False

    body = (out or "") + (("\n" + err) if err and err.strip() else "")
    body = _clip_job_output(body.strip(), _SHELL_BUDGET)
    if not body:
        body = "(no output)"
    ok = code in (0, None)
    # Say where it ran: the default is the session workspace on the node, not
    # the login directory, and relative paths are easy to misread otherwise.
    where = f"[{remote.get('name', remote_id)} · cwd {cwd}]"
    tail = (f"\n\n{where}" if ok else f"\n\n(exit code {code}) {where}")
    hint = ""
    if code == 124:
        hint = ("\n\nNOTE: only the wait timed out — the command may well still be "
                "running on the node, so do not assume it failed or had no effect. "
                "Check the state before retrying (a second run of something that "
                "half-finished is its own problem). If it is this slow by nature, "
                "it is a computation, not a shell command: submit it with "
                "submit_cluster_job and poll it.")
    elif code == 125:
        hint = ("\n\nThat directory does not exist on the node. Paths there are not "
                "the local ones: a relative path resolves against the session "
                "workspace shown above. Create it (mkdir -p) or pass the path you "
                "actually mean.")
    return f"{body}{tail}{hint}", ok


# ---------------------------------------------------------------------------
# Cluster / background jobs
# ---------------------------------------------------------------------------

def _remote_for(remote_id: str):
    from ..config import get_remote
    return get_remote(None, remote_id) if remote_id else None


def _remote_workspace(remote: dict, session) -> str:
    """Persistent per-session workspace on the compute node where heavy jobs run
    and big files / metadata live (they stay here — only lightweight results are
    fetched back for local rendering). ``<remote_dir or $HOME>/octoslave/<name>``,
    created on first use."""
    from ..remote import RemoteSession
    sess = RemoteSession.get(remote)
    base = (remote.get("remote_dir") or "").strip() or sess.home()
    name = posixpath.basename((session.working_dir or "session").rstrip("/")) or "session"
    ws = posixpath.normpath(posixpath.join(base, "octoslave", name))
    sess.mkdirs(ws)
    return ws


def _remote_job_cwd(remote: dict, session, explicit: str) -> str:
    """Resolve a job's working directory ON THE REMOTE. An absolute path is used
    as-is; a relative path is taken under the session workspace; empty defaults to
    the workspace itself. Never a local path (which would not exist on the node)."""
    ws = _remote_workspace(remote, session)
    if not explicit:
        return ws
    if posixpath.isabs(explicit):
        return explicit
    return posixpath.normpath(posixpath.join(ws, explicit))


# Commands that only LOOK at the node: listing, reading, probing the
# environment, querying the queue. A "job" made of nothing but these is not a
# computation — it is a question, and it belongs in cluster_shell where the
# answer comes straight back and the Jobs panel stays about real work.
#
# Deliberately EXCLUDES anything that can legitimately run long — interpreters,
# installers, downloads and bulk copies (python, uv, conda, pip, git, curl,
# wget, rsync, cp, tar, make …) — so a real computation is never mistaken for a
# question.
_PROBE_COMMANDS = frozenset("""
ls ll dir cat head tail less more wc stat file realpath readlink basename dirname
pwd cd echo printf true false test date uptime hostname uname whoami id groups
which whereis type command man info help env printenv export set module ml
df free lscpu lsblk nproc vmstat lsmod ulimit getconf nvidia-smi rocm-smi
sinfo squeue sacct scontrol sstat qstat qhost pbsnodes
mkdir rmdir touch chmod chgrp chown ln
grep egrep fgrep sort uniq cut tr jq column awk sed
""".split())

# A trailing `--version` / `--help` makes even a heavy binary a probe.
_PROBE_FLAGS = ("--help", "-h", "--version", "-V", "--list", "-l")


def _looks_like_a_probe(command: str) -> bool:
    """True when every part of ``command`` only inspects the node.

    Conservative on purpose: anything it cannot account for (an unknown binary,
    a script, a submit command) is treated as real work and allowed through.
    """
    text = (command or "").strip()
    if not text or "\n" in text.strip().strip(";"):
        return False                      # a multi-line script is real work
    if len(text) > 600:
        return False
    try:
        parts = shlex.split(text, comments=True)
    except ValueError:
        return False
    if not parts:
        return False
    # Split on shell separators and check the head of every segment.
    segments: list[list[str]] = [[]]
    for tok in parts:
        if tok in ("&&", "||", "|", ";", "&"):
            segments.append([])
        else:
            segments[-1].append(tok)
    saw = False
    for seg in segments:
        head = next((w for w in seg if "=" not in w), "")
        if not head:
            continue
        saw = True
        base = posixpath.basename(head)
        if base in _PROBE_COMMANDS:
            continue
        if any(f in seg for f in _PROBE_FLAGS):
            continue
        return False                      # something real in the chain
    return saw


def _run_submit_job(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _compute_ctx()
    if ctx is None:
        return "Compute context unavailable.", False

    name = str(args.get("name", "")).strip() or "job"
    command = str(args.get("command", "")).strip()
    if not command:
        return "submit_cluster_job needs a `command`.", False
    scheduler = str(args.get("scheduler", "shell")).strip().lower()
    if scheduler not in ("shell", "slurm", "pbs"):
        scheduler = "shell"
    # A scheduler submit is always a real job. A plain shell command that only
    # inspects the node is not — send it to cluster_shell so the Jobs panel keeps
    # showing the computations the researcher actually cares about.
    if scheduler == "shell" and not _truthy(args.get("force")) \
            and _looks_like_a_probe(command):
        target = str(args.get("remote_id", "")).strip() or \
            getattr(ctx.session, "remote_id", "") or ""
        where = "cluster_shell" if target else "bash"
        return (f"Not submitted — this command only inspects the node, so it is "
                f"not a job.\n\nRun it with {where} instead: the output comes "
                f"straight back and it stays out of the researcher's Jobs panel, "
                f"which is for real computations (scheduler submissions and "
                f"long-running work).\n\nIf this really is a long computation "
                f"despite how it reads, re-submit it with force=true.", False)
    remote_id = str(args.get("remote_id", "")).strip() or ctx.session.remote_id or ""
    remote = _remote_for(remote_id)
    # On a remote node the job runs in the persistent session workspace (big files
    # stay there); locally it uses the given cwd or the session dir.
    if remote:
        cwd = _remote_job_cwd(remote, ctx.session, str(args.get("cwd", "")).strip())
    else:
        cwd = str(args.get("cwd", "")).strip() or working_dir
    label = remote.get("name", remote_id) if remote else "local"

    job = Job(name=name, command=command, remote_id=remote_id or None,
              remote_label=label, scheduler=scheduler, cwd=cwd)

    try:
        if remote:
            handle, out = _submit_remote(remote, command, cwd, scheduler, job.id)
        else:
            handle, out = _submit_local(command, cwd, scheduler, job.id, ctx)
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.output = str(exc)
        ctx.session.add_job(job)
        _emit_job(job)
        return f"Failed to submit '{name}': {exc}", False

    job.handle = handle
    job.output = out
    if not handle:
        # No PID / scheduler id came back, so the submit itself did not take
        # (a timeout on the connection, a rejected script). Recording it as
        # "running" would be a lie that polling then turns into "done", because
        # an empty handle names no live process — report the failure instead.
        job.status = "failed"
        ctx.session.add_job(job)
        _emit_job(job)
        return (f"Job '{name}' was NOT submitted on {label}: no job id came back. "
                f"The node said:\n{_clip_job_output(out, 2000) or '(nothing)'}\n\n"
                f"Fix that before retrying — nothing is running.", False)
    job.status = "running"
    ctx.session.add_job(job)
    _emit_job(job)
    return (f"Submitted job '{name}' on {label} "
            f"({scheduler}, handle={handle}, id={job.id}). "
            f"Poll it with check_cluster_job(job_id='{job.id}').", True)


def _submit_remote(remote: dict, command: str, cwd: str, scheduler: str,
                   jid: str) -> tuple[str, str]:
    from ..remote import RemoteSession
    sess = RemoteSession.get(remote)
    if scheduler == "slurm":
        # Command is expected to be (or point at) a submit script.
        out, err, code = sess.run(f"sbatch {command}", cwd, timeout=180)
        handle = _parse_slurm_id(out)
        return handle, (out + err).strip()
    if scheduler == "pbs":
        out, err, code = sess.run(f"qsub {command}", cwd, timeout=180)
        return out.strip().split()[0] if out.strip() else "", (out + err).strip()
    # shell: launch detached, capture PID, tee to a log file
    log = f"science_{jid}.log"
    # shlex.quote, not json.dumps: JSON escapes are not shell escapes, so a
    # newline arrived as a literal "\n" (fusing the lines of a multi-line
    # command into one) and non-ASCII text as "\uXXXX".
    qcwd = shlex.quote(cwd)
    wrapped = (f"mkdir -p {qcwd} && cd {qcwd} && "
               f"nohup sh -c {shlex.quote(command)} > {log} 2>&1 & echo $!")
    # Launching is quick, but the host's setup snippet and the login shell run
    # first; 60s was not always enough, and a submit that times out looks like a
    # job that never started.
    out, err, code = sess.run(wrapped, cwd, timeout=180)
    return _parse_pid(out), (out + err).strip()


def _parse_pid(text: str) -> str:
    """The PID echoed by a detached launch.

    Takes the LAST all-digit token rather than the whole of stdout: a login
    banner, an `ml`/module notice or a locale warning ahead of it would
    otherwise be stored as the handle, and every later poll of that job would
    look at a process that does not exist.
    """
    for tok in reversed((text or "").split()):
        if tok.isdigit():
            return tok
    return ""


def _submit_local(command: str, cwd: str, scheduler: str, jid: str,
                  ctx) -> tuple[str, str]:
    Path(cwd).mkdir(parents=True, exist_ok=True)
    logdir = ctx.session.science_dir / "jobs"
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"{jid}.log"
    if scheduler == "slurm":
        r = subprocess.run(["sbatch", command], cwd=cwd, capture_output=True,
                           text=True, timeout=120)
        return _parse_slurm_id(r.stdout), (r.stdout + r.stderr).strip()
    # shell background
    fh = open(logf, "w")
    proc = subprocess.Popen(command, cwd=cwd, shell=True, stdout=fh,
                            stderr=subprocess.STDOUT)
    return str(proc.pid), f"started (pid {proc.pid}), log: {logf}"


def _parse_slurm_id(text: str) -> str:
    # "Submitted batch job 12345"
    for tok in (text or "").split():
        if tok.isdigit():
            return tok
    return ""


# How much of a job's log a poll may return. A tail-only clip loses the START of
# the output, which for a probe/setup job is exactly the part that matters (the
# hostname, the GPU, the versions) — the model then re-submits the same job to
# see it, and can lose it again. Clip the MIDDLE instead and say so.
_JOB_OUT_BUDGET = 6000


def _clip_job_output(text: str, budget: int = _JOB_OUT_BUDGET) -> str:
    """Keep the head and the tail of ``text``, marking what was dropped."""
    if len(text) <= budget:
        return text
    head = budget // 3
    tail = budget - head
    return (f"{text[:head]}\n"
            f"… [{len(text) - budget:,} characters omitted from the middle — "
            f"read the log itself if you need them] …\n"
            f"{text[-tail:]}")


def _run_check_job(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _compute_ctx()
    if ctx is None:
        return "Compute context unavailable.", False
    jid = str(args.get("job_id", "")).strip()
    job = ctx.session.get_job(jid)
    if job is None:
        return f"No job '{jid}'. Known jobs: " + \
               ", ".join(f"{j.name}({j.id})" for j in ctx.session.jobs), False

    try:
        lines = max(1, min(1000, int(args.get("lines") or 120)))
    except (TypeError, ValueError):
        lines = 120
    remote = _remote_for(job.remote_id or "")
    try:
        status, out = _poll_job(job, remote, ctx, lines=lines)
    except Exception as exc:  # noqa: BLE001
        status, out = "unknown", str(exc)

    job.status = status
    if out:
        job.output = _clip_job_output(out, 4000)
    # persist by re-adding through the session lock path
    for j in ctx.session.jobs:
        if j.id == job.id:
            j.status, j.output = job.status, job.output
    ctx.session.save()
    _emit_job(job)
    return (f"Job '{job.name}' ({job.id}) on {job.remote_label}: {status}. "
            f"Log: {_job_log_path(job, remote, ctx)}\n"
            f"{_clip_job_output(out) if out else ''}", True)


def _job_log_path(job: Job, remote, ctx) -> str:
    """Where this job's output is being written (on the node, or locally)."""
    if job.scheduler == "slurm":
        return f"(scheduler output under {job.cwd})"
    if remote:
        return posixpath.join(job.cwd or ".", f"science_{job.id}.log")
    return str(ctx.session.science_dir / "jobs" / f"{job.id}.log")


def _poll_job(job: Job, remote, ctx, lines: int = 120) -> tuple[str, str]:
    if job.scheduler == "slurm":
        cmd = (f"sacct -j {job.handle} --format=State,Elapsed,ExitCode "
               f"--noheader --parsable2 2>/dev/null | head -1 || "
               f"squeue -j {job.handle} -h -o %T")
        out = _run_anywhere(cmd, job.cwd, remote)
        s = out.strip().lower()
        if not s:
            return "done", out
        if "running" in s or "pending" in s:
            return "running", out
        if "completed" in s:
            return "done", out
        if "fail" in s or "cancel" in s or "timeout" in s:
            return "failed", out
        return "unknown", out
    # shell: is the pid alive? tail the log. A finished-but-unreaped LOCAL child
    # lingers as a zombie (state 'Z'), which `kill -0` still reports as alive — so
    # the job would appear to run forever. Check the process STATE instead and
    # treat an empty state (gone) or a zombie as done. Works for remote too (a
    # done nohup'd job is reaped by init → empty state).
    log = (f"science_{job.id}.log" if remote else
           str(ctx.session.science_dir / "jobs" / f"{job.id}.log"))
    if not job.handle:
        # Nothing was ever launched (the submit failed). `ps -p ` with no pid
        # matches nothing, which would read as "finished" — say what is true.
        return "failed", (job.output or "the submit returned no process id")
    alive_cmd = (f"S=$(ps -o stat= -p {job.handle} 2>/dev/null | tr -d ' '); "
                 f"case \"$S\" in ''|Z*) echo GONE;; *) echo ALIVE;; esac")
    alive = _run_anywhere(alive_cmd, job.cwd, remote).strip()
    tail = _run_anywhere(f"tail -n {lines} {shlex.quote(log)} 2>/dev/null", job.cwd, remote)
    status = "running" if "ALIVE" in alive else "done"
    return status, tail


def _run_anywhere(cmd: str, cwd: str, remote) -> str:
    if remote:
        from ..remote import RemoteSession
        out, err, _ = RemoteSession.get(remote).run(cmd, cwd, timeout=60)
        return out + err
    r = subprocess.run(cmd, cwd=cwd or None, shell=True, capture_output=True,
                       text=True, timeout=60)
    return r.stdout + r.stderr


def _emit_job(job: Job) -> None:
    _emit_compute({"type": "science_job", "id": job.id, "name": job.name,
                   "status": job.status, "remote": job.remote_label,
                   "scheduler": job.scheduler, "handle": job.handle,
                   "output": job.output[-1500:]})


def _run_write_cluster_file(args: dict, working_dir: str) -> tuple[str, bool]:
    """Write text straight to a file ON the compute node.

    Without this there is no way to put a script, module, or config on the node:
    submit_cluster_job only takes a command, and fetch_cluster_file only goes the
    other way. Agents therefore improvised — building the tree in the LOCAL /tmp
    and shelling out to their own `ssh` — which puts work back on the machine the
    user asked to keep free, and hides those commands from the Jobs panel.
    """
    ctx = _compute_ctx()
    if ctx is None:
        return "Compute context unavailable.", False
    raw = str(args.get("path", "") or args.get("remote_path", "")).strip()
    if not raw:
        return "write_cluster_file needs a `path` (on the compute node).", False
    content = args.get("content")
    if content is None:
        return "write_cluster_file needs `content` (the text to write).", False
    if not isinstance(content, str):
        content = json.dumps(content, indent=2)
    remote_id = str(args.get("remote_id", "")).strip() or ctx.session.remote_id or ""
    remote = _remote_for(remote_id)
    if not remote:
        return ("write_cluster_file needs a remote compute node, but none is "
                "selected for this session — write the file locally instead.", False)
    from ..remote import RemoteSession
    sess = RemoteSession.get(remote)
    ws = _remote_workspace(remote, ctx.session)
    remote_path = raw if posixpath.isabs(raw) else posixpath.join(ws, raw)
    try:
        parent = posixpath.dirname(remote_path)
        if parent:
            sess.mkdirs(parent)
        sess.write_text(remote_path, content)
        if str(args.get("executable", "")).strip().lower() in ("1", "true", "yes"):
            sess.run(f"chmod +x {shlex.quote(remote_path)}", None, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return f"write_cluster_file: failed to write {remote_path}: {exc}", False
    return (f"Wrote {len(content):,} characters to '{remote_path}' on "
            f"{remote.get('name', remote_id)}. Run it with submit_cluster_job "
            f"(paths relative to the session workspace {ws}).", True)


def _run_fetch_cluster_file(args: dict, working_dir: str) -> tuple[str, bool]:
    """Copy a (lightweight) result file from the compute node back to the LOCAL
    session directory so it can be rendered/presented. Big data stays on the node;
    this is for the plots, projections, and small tables the user should see."""
    ctx = _compute_ctx()
    if ctx is None:
        return "Compute context unavailable.", False
    raw = str(args.get("path", "") or args.get("remote_path", "")).strip()
    if not raw:
        return "fetch_cluster_file needs a `path` to a file on the cluster.", False
    remote_id = str(args.get("remote_id", "")).strip() or ctx.session.remote_id or ""
    remote = _remote_for(remote_id)
    if not remote:
        return ("fetch_cluster_file needs a remote compute node, but none is "
                "selected for this session.", False)
    from ..remote import RemoteSession
    sess = RemoteSession.get(remote)
    # Relative paths resolve against the session workspace on the node.
    remote_path = raw if posixpath.isabs(raw) else \
        posixpath.join(_remote_workspace(remote, ctx.session), raw)
    if not sess.is_file(remote_path):
        return (f"fetch_cluster_file: no such file on "
                f"{remote.get('name', remote_id)}: {remote_path}", False)
    dest_name = str(args.get("dest", "")).strip() or posixpath.basename(remote_path)
    local_dest = Path(ctx.session.working_dir) / dest_name
    try:
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        sess.pull(remote_path, dest=str(local_dest))
    except Exception as exc:  # noqa: BLE001
        return f"fetch_cluster_file: failed to copy {remote_path}: {exc}", False
    return (f"Fetched '{remote_path}' from {remote.get('name', remote_id)} to local "
            f"'{dest_name}'. Render it for the user with "
            f"present_output(path='{dest_name}').", True)


# ---------------------------------------------------------------------------
# Present output (inline artifact card) + provenance + curated dataset
# ---------------------------------------------------------------------------

def _run_present_output(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _ctx.current()
    if ctx is None:
        return "Science context unavailable.", False
    raw = str(args.get("path", "")).strip()
    if not raw:
        return "present_output needs a `path`.", False
    p = Path(raw)
    if not p.is_absolute():
        p = Path(working_dir) / raw
    if not p.exists():
        return f"present_output: no such file '{raw}'.", False
    kind = str(args.get("kind", "")).strip() or _infer_kind(p)
    art = Artifact(path=str(p.resolve()), rel=_relpath(p, working_dir),
                   caption=str(args.get("caption", "")).strip(), kind=kind,
                   provenance=str(args.get("provenance", "")).strip(),
                   interim=_truthy(args.get("interim")),
                   # Set when a specialist presents its own result (see _speaker);
                   # per-thread, so concurrent specialists are not confused.
                   by=_ctx.speaker().get("agent_name", ""))
    art = ctx.session.add_artifact(art)
    _ctx.emit({"type": "science_artifact", "id": art.id, "rel": art.rel,
               "path": art.path, "caption": art.caption, "kind": art.kind,
               "provenance": art.provenance, "interim": art.interim})
    out = (f"Presented '{art.rel}' to the user (id={art.id})"
           + (" as an interim preview" if art.interim else "")
           + ". They can comment on it to request refinements.")
    # Showing a page or figure nobody has looked at is how a broken layout, an
    # unloaded viewer or an empty plot reaches the researcher first. Only for
    # final outputs: an interim look is explicitly a rough one.
    if not art.interim and p.suffix.lower() in _workspace.VISUAL_SUFFIXES:
        try:
            seen = _tools.viewed_at(str(p))
            changed = p.stat().st_mtime
        except OSError:
            seen, changed = None, 0.0
        if seen is None or seen < changed:
            out += ("\n\nYou have not looked at this since it last changed. Call "
                    f"view_image(path='{art.rel}') now — for a page it renders in "
                    "a browser and reports the layout, console errors and any "
                    "images that fail to load. Fix what it shows before treating "
                    "this as final.")
    # A finished output with nothing in the ledger is the usual way provenance
    # ends up half-empty: it is always the next thing to do, and never done. Say
    # so at the moment the output appears, while the method is still at hand.
    if not art.interim:
        recorded = {str(e.get("artifact", "")).strip()
                    for e in ctx.session.provenance}
        if art.rel not in recorded and art.path not in recorded:
            out += ("\n\nNot in the provenance ledger yet. Call record_provenance "
                    f"for it now (artifact='{art.rel}') with the inputs it came "
                    "from and the method that made it — a result nobody can "
                    "reproduce is not finished, and the detail is hardest to "
                    "reconstruct later.")
    return out, True


def _run_record_provenance(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _ctx.current()
    if ctx is None:
        return "Science context unavailable.", False
    entry = {
        "artifact": str(args.get("artifact", "")).strip(),
        "method": str(args.get("method", "")).strip(),
        "inputs": str(args.get("inputs", "")).strip(),
        "notes": str(args.get("notes", "")).strip(),
    }
    if not entry["artifact"]:
        return "record_provenance needs an `artifact` name/path.", False
    ctx.session.add_provenance(entry)
    _ctx.emit({"type": "science_provenance", "entry": entry})
    return (f"Recorded provenance for '{entry['artifact']}' in "
            f"science/PROVENANCE.md (FAIR ledger).", True)


def _run_curate_dataset(args: dict, working_dir: str) -> tuple[str, bool]:
    """Wrap a data file as a FAIR dataset: emit a Frictionless-style
    datapackage.json beside it and surface it as an artifact."""
    ctx = _ctx.current()
    if ctx is None:
        return "Science context unavailable.", False
    raw = str(args.get("path", "")).strip()
    if not raw:
        return "curate_dataset needs a `path` to the curated data file.", False
    p = Path(raw)
    if not p.is_absolute():
        p = Path(working_dir) / raw
    if not p.exists():
        return f"curate_dataset: no such file '{raw}'.", False

    name = str(args.get("name", "")).strip() or p.stem
    fields = args.get("fields") or []
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except Exception:
            fields = [f.strip() for f in fields.split(",") if f.strip()]
    resources = [{
        "name": name,
        "path": p.name,
        "format": p.suffix.lstrip("."),
        "schema": {"fields": [
            ({"name": f} if isinstance(f, str) else f) for f in fields
        ]} if fields else {},
    }]
    pkg = {
        "name": name.lower().replace(" ", "-"),
        "title": str(args.get("title", "")).strip() or name,
        "description": str(args.get("description", "")).strip(),
        "sources": args.get("sources") or [],
        "licenses": args.get("licenses") or [{"name": "CC-BY-4.0"}],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "resources": resources,
    }
    dp = p.parent / "datapackage.json"
    dp.write_text(json.dumps(pkg, indent=2))

    art = Artifact(path=str(p.resolve()), rel=_relpath(p, working_dir),
                   caption=f"Curated dataset: {name}", kind="dataset",
                   provenance=str(args.get("description", "")).strip())
    art = ctx.session.add_artifact(art)
    ctx.session.add_provenance({
        "artifact": name, "method": "curated into FAIR datapackage",
        "inputs": str(args.get("sources") or raw),
        "notes": f"datapackage.json written to {dp}",
    })
    _ctx.emit({"type": "science_artifact", "id": art.id, "rel": art.rel,
               "path": art.path, "caption": art.caption, "kind": art.kind,
               "provenance": art.provenance})
    return (f"Curated '{name}' as a FAIR dataset (datapackage.json at {dp}) and "
            f"surfaced it (id={art.id}).", True)


# ---------------------------------------------------------------------------
# Literature / knowledge search (Europe PMC — open, no key)
# ---------------------------------------------------------------------------

def _run_literature_search(args: dict, working_dir: str) -> tuple[str, bool]:
    query = str(args.get("query", "")).strip()
    if not query:
        return "literature_search needs a `query`.", False
    limit = max(1, min(25, int(args.get("limit", 8) or 8)))
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": query, "format": "json", "pageSize": limit,
              "resultType": "core"}
    try:
        data = _http_json(url, params)
    except Exception as exc:  # noqa: BLE001
        return f"literature_search failed: {exc}", False
    results = (data.get("resultList") or {}).get("result") or []
    if not results:
        return f"No literature hits for '{query}'.", True
    lines = [f"Top {len(results)} results for '{query}' (Europe PMC):", ""]
    for r in results:
        authors = r.get("authorString", "")
        cite = r.get("citedByCount", 0)
        doi = r.get("doi", "")
        ident = doi or f"{r.get('source','')}:{r.get('id','')}"
        lines.append(
            f"- {r.get('title','(untitled)').rstrip('.')} "
            f"({r.get('pubYear','?')}). {authors[:120]}"
            f" — cited {cite}× — {ident}")
    return "\n".join(lines), True


def _http_json(url: str, params: dict) -> dict:
    try:
        import requests
        r = requests.get(url, params=params, timeout=30,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    except ImportError:
        import urllib.parse
        import urllib.request
        full = url + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(full, timeout=30) as resp:
            return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Definitions + registration
# ---------------------------------------------------------------------------

SCIENCE_TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "spawn_specialist",
        "description": "Dispatch a focused specialist agent to carry out a bounded "
                       "sub-task (e.g. a Structural Biologist, a Data Wrangler) on "
                       "its own fresh context with the tools you grant. It runs IN "
                       "THE BACKGROUND and this returns immediately, so you stay free "
                       "to talk to the researcher, dispatch more specialists and work "
                       "yourself while it goes. Its report is delivered to you "
                       "automatically when it finishes. Prefer this over doing a "
                       "multi-step chunk of work inline: anything needing more than a "
                       "handful of tool calls, detail work you don't need to watch, "
                       "an independent piece of the plan, or expertise you'd "
                       "improvise. Dispatch independent pieces TOGETHER — they run in "
                       "parallel. ONE specialist per area of work: to take an existing "
                       "one further, use continue_specialist instead of spawning a "
                       "second agent.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Short display name."},
            "role": {"type": "string", "description": "One-line role title."},
            "goal": {"type": "string", "description": "What they must accomplish."},
            "task": {"type": "string", "description": "Concrete instructions to execute now."},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "Allowlist of tool names to grant (from the tool registry)."},
            "icon": {"type": "string", "description": "An emoji for the UI (optional)."},
            "model": {"type": "string", "description": "Which model this specialist "
                      "should run on — choose from the configured specialist pool "
                      "(listed in your system prompt). Omit to use the default."},
            "force": {"type": "boolean", "description": "Spawn even though an existing "
                      "specialist covers a similar remit. Only when this is genuinely "
                      "a different angle — say how in the goal."},
            "wait": {"type": "boolean", "description": "Block until it finishes "
                     "instead of running it in the background. Only when you truly "
                     "cannot do anything — not even talk to the researcher — until "
                     "its result is in. Normally leave this out."},
        }, "required": ["name", "goal"]}}},
    {"type": "function", "function": {
        "name": "continue_specialist",
        "description": "Give MORE work to a specialist you already spawned. It resumes "
                       "with its full previous transcript — the sources it tried, what "
                       "worked, and the dead ends it hit — so it continues instead of "
                       "starting over. Always prefer this over spawning a second agent "
                       "for the same area (incomplete results, a follow-up question, a "
                       "correction, or a next stage of the same work).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "The specialist's id (or its name)."},
            "task": {"type": "string", "description": "The next concrete instruction. "
                     "Reference what it already did; don't restate the whole goal."},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "Extra tools to grant for this leg (optional)."},
            "model": {"type": "string", "description": "Run this leg on a different "
                      "model from the pool (optional)."},
            "wait": {"type": "boolean", "description": "Block until it finishes "
                     "instead of running it in the background. Normally leave out."},
        }, "required": ["id", "task"]}}},
    {"type": "function", "function": {
        "name": "check_specialists",
        "description": "See what your background specialists are doing right now — "
                       "how long each has been working, how many steps it has taken, "
                       "what it is running — and pick up any report that has landed. "
                       "You do NOT need this to receive results: a specialist's report "
                       "reaches you on its own when it finishes. Use it to tell the "
                       "researcher where things stand, or with wait=true to park when "
                       "you genuinely have nothing else to do. Never poll it in a loop.",
        "parameters": {"type": "object", "properties": {
            "wait": {"type": "boolean", "description": "Block until a specialist "
                     "reports back (or the researcher messages you, or the timeout "
                     "expires). Only when you have nothing else to get on with."},
            "timeout": {"type": "integer", "description": "Seconds to wait with "
                        "wait=true (default 300, max 1800)."},
        }}}},
    {"type": "function", "function": {
        "name": "cluster_shell",
        "description": "Run a SHORT command on the compute node and get its output "
                       "back straight away — listing a directory, checking available "
                       "modules or queues, reading a config, inspecting a result, "
                       "preparing a directory. This is the normal way to interact with "
                       "the node. It is NOT a job: nothing is recorded in the "
                       "researcher's Jobs panel, which stays about real computations. "
                       "Use submit_cluster_job only for work that actually takes a "
                       "long time.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command to run."},
            "cwd": {"type": "string", "description": "Working directory on the node. "
                    "A relative path resolves against the session workspace."},
            "timeout": {"type": "integer", "description": "Seconds before giving up "
                        "(default 120, max 600). Needing more than this means it is a "
                        "job, not a shell command."},
            "remote_id": {"type": "string", "description": "Configured remote id "
                          "(optional; defaults to the session's compute node)."},
        }, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "submit_cluster_job",
        "description": "Submit a HEAVY, long-running computation — a scheduler job "
                       "(Slurm/PBS), or work launched detached on the node or locally "
                       "that runs for minutes or hours. Returns a job id to poll with "
                       "check_cluster_job; never block on a long computation with "
                       "bash. This is what fills the researcher's Jobs panel, so it is "
                       "ONLY for real computations: to look around the node, read a "
                       "file, check modules or queues, or set a directory up, use "
                       "cluster_shell (or bash when the work is local) — those are not "
                       "jobs and a submit of one will be refused.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "command": {"type": "string", "description": "Command or submit-script path."},
            "scheduler": {"type": "string", "enum": ["shell", "slurm", "pbs"],
                          "description": "The scheduler to submit through; 'shell' "
                          "launches it detached (a long local or on-node run)."},
            "remote_id": {"type": "string", "description": "Configured remote id; omit for local."},
            "cwd": {"type": "string", "description": "Working directory for the job."},
            "force": {"type": "boolean", "description": "Submit anyway when this was "
                      "refused as 'not a job' but genuinely is a long computation."},
        }, "required": ["name", "command"]}}},
    {"type": "function", "function": {
        "name": "check_cluster_job",
        "description": "Check the status and read the output of a job submitted with "
                       "submit_cluster_job. Long output is clipped in the MIDDLE (head "
                       "and tail are both kept) and the log's path is returned, so "
                       "there is never a need to re-submit a job just to see what it "
                       "printed.",
        "parameters": {"type": "object", "properties": {
            "job_id": {"type": "string"},
            "lines": {"type": "integer", "description": "How many trailing log lines "
                      "to read (default 120, max 1000)."},
        }, "required": ["job_id"]}}},
    {"type": "function", "function": {
        "name": "write_cluster_file",
        "description": "Write a file (script, module, config, submit script) directly "
                       "ON the compute node — nothing is created on the local machine. "
                       "This is how you put code on the node: build the project there "
                       "with write_cluster_file, then run it with submit_cluster_job. "
                       "Do NOT assemble files in the local /tmp and ssh them across by "
                       "hand — that loads the machine the user wants left free and the "
                       "commands never appear in the Jobs panel.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path on the node. A relative "
                     "path resolves against the session workspace; parent directories "
                     "are created."},
            "content": {"type": "string", "description": "Full text of the file."},
            "executable": {"type": "boolean", "description": "chmod +x it (for scripts)."},
            "remote_id": {"type": "string", "description": "Configured remote id "
                          "(optional; defaults to the session's compute node)."},
        }, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "fetch_cluster_file",
        "description": "Copy a LIGHTWEIGHT result file (a plot, a UMAP/embedding "
                       "projection, a small summary table) from the remote compute "
                       "node back to the local session directory so you can render it "
                       "with present_output. Big data and intermediates stay on the "
                       "node — only fetch what the user should actually see.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path on the cluster. "
                     "Relative paths resolve against the job's session workspace."},
            "dest": {"type": "string", "description": "Local filename to save as "
                     "(optional; defaults to the remote basename)."},
            "remote_id": {"type": "string", "description": "Configured remote id "
                          "(optional; defaults to the session's compute node)."},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "present_output",
        "description": "Surface a file (plot, table, report, dataset) to the researcher: "
                       "it appears in the conversation and opens in their live "
                       "workspace, where they can comment to refine it. Call this "
                       "whenever something they should see exists — including early "
                       "looks at work in progress (interim=true). Presenting the same "
                       "path again updates that card in place.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the output file."},
            "caption": {"type": "string"},
            "kind": {"type": "string", "enum": ["image", "table", "report", "dataset", "text", "file"]},
            "provenance": {"type": "string", "description": "One line on how it was made."},
            "interim": {"type": "boolean", "description": "True for a preliminary look "
                        "(first-pass plot, partial table) while the work continues; "
                        "present the same path again without it once final."},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "record_provenance",
        "description": "Record how one output was produced, in science/PROVENANCE.md, "
                       "so it can be regenerated or checked later. Call it for every "
                       "output you present (the same artifact path again replaces its "
                       "entry, so record it once and refine it if the method changes).",
        "parameters": {"type": "object", "properties": {
            "artifact": {"type": "string", "description": "The output this is about — "
                         "the same path you gave present_output, so the two line up."},
            "method": {"type": "string", "description": "How it was made: the steps, "
                       "tools and the parameters that would change the result. Enough "
                       "that someone could repeat it without guessing."},
            "inputs": {"type": "string", "description": "What it was derived from: "
                       "data files, structures, accessions, earlier outputs — with "
                       "identifiers, not just descriptions."},
            "notes": {"type": "string", "description": "What the result shows, and any "
                      "caveat or limitation a reader should know before trusting it."},
        }, "required": ["artifact"]}}},
    {"type": "function", "function": {
        "name": "curate_dataset",
        "description": "Turn a curated data file into a FAIR dataset: writes a "
                       "Frictionless datapackage.json (schema, sources, licence) beside "
                       "it and surfaces it as an artifact. Use after cleaning messy data.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the curated data file."},
            "name": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "object"},
                       "description": "Column schema: [{name, type, description}, …]."},
            "sources": {"type": "array", "items": {"type": "object"}},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "literature_search",
        "description": "Search the scientific literature (Europe PMC: PubMed + preprints "
                       "+ agricola) for the most relevant current knowledge on a topic.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        }, "required": ["query"]}}},
]

SCIENCE_TOOL_NAMES = frozenset(
    t["function"]["name"] for t in SCIENCE_TOOL_DEFINITIONS)

_RUNNERS = {
    "spawn_specialist": _run_spawn_specialist,
    "continue_specialist": _run_continue_specialist,
    "check_specialists": _run_check_specialists,
    "cluster_shell": _run_cluster_shell,
    "submit_cluster_job": _run_submit_job,
    "check_cluster_job": _run_check_job,
    "write_cluster_file": _run_write_cluster_file,
    "fetch_cluster_file": _run_fetch_cluster_file,
    "present_output": _run_present_output,
    "record_provenance": _run_record_provenance,
    "curate_dataset": _run_curate_dataset,
    "literature_search": _run_literature_search,
}


# The tool registry is process-wide, but several runs can use these tools at
# once: two Science sessions, or a Science turn beside a Lab run that borrows the
# cluster tools. So each tool is counted per user and leaves the registry only
# when the LAST one releases it — otherwise the first run to finish would pull
# the tools out from under the others mid-run ("Unknown tool").
_REG_LOCK = threading.Lock()
_REG_USERS: dict[str, int] = {}


def _acquire(names) -> None:
    with _REG_LOCK:
        for d in SCIENCE_TOOL_DEFINITIONS:
            name = d["function"]["name"]
            if name in names:
                _REG_USERS[name] = _REG_USERS.get(name, 0) + 1
                _tools.register_dynamic_tool(d, _RUNNERS[name])


def _release(names) -> None:
    with _REG_LOCK:
        for name in names:
            left = _REG_USERS.get(name, 0) - 1
            if left > 0:
                _REG_USERS[name] = left
            else:
                _REG_USERS.pop(name, None)
                _tools.unregister_dynamic_tool(name)


def register() -> None:
    """Register the science tools into the live dynamic-tool registry."""
    _acquire(SCIENCE_TOOL_NAMES)


def unregister() -> None:
    _release(SCIENCE_TOOL_NAMES)


# The cluster-job subset — reused by the Lab (which does not want the rest of the
# science toolset). Backed by the thread-local compute context (set_compute_context).
CLUSTER_TOOL_NAMES = ("submit_cluster_job", "check_cluster_job", "cluster_shell",
                      "write_cluster_file", "fetch_cluster_file")


def register_cluster_tools() -> None:
    """Register ONLY the cluster-job tools into the live registry (Lab use)."""
    _acquire(CLUSTER_TOOL_NAMES)


def unregister_cluster_tools() -> None:
    _release(CLUSTER_TOOL_NAMES)
