"""Science capability tools, registered as dynamic tools while a run is active.

Signature contract (same as the Lab foundry): ``run(args, working_dir) ->
(text, ok)``. Run context (session/client/model/emit) comes from
``science.context``.
"""

from __future__ import annotations

import json
import posixpath
import subprocess
import threading
import time
from pathlib import Path

from .. import tools as _tools
from . import context as _ctx
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
# spawn_specialist
# ---------------------------------------------------------------------------

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

    # Model for this specialist: the orchestrator may choose from the configured
    # pool. An unknown/blank choice falls back to a pool member (or, if no pool is
    # set, the orchestrator's own model). This lets a strong orchestrator delegate
    # to cheaper/faster/specialised models per task.
    pool = list(getattr(ctx, "specialist_models", []) or [])
    requested = str(args.get("model", "")).strip()
    if requested and (not pool or requested in pool):
        spec_model = requested
    elif pool:
        spec_model = pool[0]
    else:
        spec_model = ctx.model

    from ..lab.state import AgentSpec
    from ..lab.agent_runtime import run_agent_task

    spec = AgentSpec(name=name, role=role, expertise=role, goal=goal or task,
                     tools=granted, model=spec_model, icon=icon)

    rec = Specialist(name=name, role=role, goal=goal or task, tools=granted,
                     icon=icon, status="working")
    ctx.session.add_specialist(rec)
    _ctx.emit({"type": "science_specialist", "event": "start", "id": rec.id,
               "name": name, "role": role, "goal": goal or task,
               "tools": granted, "icon": icon, "model": spec_model})

    try:
        _transcript, summary = run_agent_task(
            spec, task or goal, working_dir, ctx.client,
            model=spec_model, permission_mode=ctx.permission_mode,
            context=f"You are a specialist reporting to a research orchestrator. "
                    f"Stay strictly within your goal. When you produce a figure, "
                    f"table, or report the user should see, note its exact path.",
            emit=ctx.emit,
        )
    except Exception as exc:  # noqa: BLE001
        ctx.session.update_specialist(rec.id, status="failed",
                                      summary=f"error: {exc}")
        _ctx.emit({"type": "science_specialist", "event": "done", "id": rec.id,
                   "status": "failed", "summary": str(exc)})
        return f"Specialist '{name}' failed: {exc}", False

    summary = (summary or "").strip() or "(no summary returned)"
    ctx.session.update_specialist(rec.id, status="done", summary=summary)
    _ctx.emit({"type": "science_specialist", "event": "done", "id": rec.id,
               "status": "done", "summary": summary})
    return (f"Specialist '{name}' ({role}) finished.\n\n{summary}", True)


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
    job.status = "running"
    job.output = out
    ctx.session.add_job(job)
    _emit_job(job)
    return (f"Submitted job '{name}' on {label} "
            f"({scheduler}, handle={handle or '?'}, id={job.id}). "
            f"Poll it with check_cluster_job(job_id='{job.id}').", True)


def _submit_remote(remote: dict, command: str, cwd: str, scheduler: str,
                   jid: str) -> tuple[str, str]:
    from ..remote import RemoteSession
    sess = RemoteSession.get(remote)
    if scheduler == "slurm":
        # Command is expected to be (or point at) a submit script.
        out, err, code = sess.run(f"sbatch {command}", cwd, timeout=120)
        handle = _parse_slurm_id(out)
        return handle, (out + err).strip()
    if scheduler == "pbs":
        out, err, code = sess.run(f"qsub {command}", cwd, timeout=120)
        return out.strip().split()[0] if out.strip() else "", (out + err).strip()
    # shell: launch detached, capture PID, tee to a log file
    log = f"science_{jid}.log"
    wrapped = (f"mkdir -p '{cwd}' && cd '{cwd}' && "
               f"nohup sh -c {json.dumps(command)} > {log} 2>&1 & echo $!")
    out, err, code = sess.run(wrapped, cwd, timeout=60)
    return out.strip(), (out + err).strip()


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


def _run_check_job(args: dict, working_dir: str) -> tuple[str, bool]:
    ctx = _compute_ctx()
    if ctx is None:
        return "Compute context unavailable.", False
    jid = str(args.get("job_id", "")).strip()
    job = ctx.session.get_job(jid)
    if job is None:
        return f"No job '{jid}'. Known jobs: " + \
               ", ".join(f"{j.name}({j.id})" for j in ctx.session.jobs), False

    remote = _remote_for(job.remote_id or "")
    try:
        status, out = _poll_job(job, remote, ctx)
    except Exception as exc:  # noqa: BLE001
        status, out = "unknown", str(exc)

    job.status = status
    if out:
        job.output = out[-4000:]
    # persist by re-adding through the session lock path
    for j in ctx.session.jobs:
        if j.id == job.id:
            j.status, j.output = job.status, job.output
    ctx.session.save()
    _emit_job(job)
    return (f"Job '{job.name}' ({job.id}) on {job.remote_label}: {status}.\n"
            f"{out[-1500:] if out else ''}", True)


def _poll_job(job: Job, remote, ctx) -> tuple[str, str]:
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
    alive_cmd = (f"S=$(ps -o stat= -p {job.handle} 2>/dev/null | tr -d ' '); "
                 f"case \"$S\" in ''|Z*) echo GONE;; *) echo ALIVE;; esac")
    alive = _run_anywhere(alive_cmd, job.cwd, remote).strip()
    tail = _run_anywhere(f"tail -n 40 {log} 2>/dev/null", job.cwd, remote)
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
                   provenance=str(args.get("provenance", "")).strip())
    art = ctx.session.add_artifact(art)
    _ctx.emit({"type": "science_artifact", "id": art.id, "rel": art.rel,
               "path": art.path, "caption": art.caption, "kind": art.kind,
               "provenance": art.provenance})
    return (f"Presented '{art.rel}' to the user (id={art.id}). They can comment "
            f"on it inline to request refinements.", True)


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
        "description": "Spin up a focused specialist agent to carry out a bounded "
                       "sub-task (e.g. a Structural Biologist, a Data Wrangler). It "
                       "runs to completion with the tools you grant and returns a "
                       "summary. Use this to parallelise or to bring in expertise.",
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
        }, "required": ["name", "goal"]}}},
    {"type": "function", "function": {
        "name": "submit_cluster_job",
        "description": "Submit a long-running job to a remote HPC cluster (Slurm/PBS) "
                       "or run it detached in the background. Returns a job id you can "
                       "poll — do NOT block on long computations with bash.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "command": {"type": "string", "description": "Command or submit-script path."},
            "scheduler": {"type": "string", "enum": ["shell", "slurm", "pbs"]},
            "remote_id": {"type": "string", "description": "Configured remote id; omit for local."},
            "cwd": {"type": "string", "description": "Working directory for the job."},
        }, "required": ["name", "command"]}}},
    {"type": "function", "function": {
        "name": "check_cluster_job",
        "description": "Check the status and tail the output of a job submitted with "
                       "submit_cluster_job.",
        "parameters": {"type": "object", "properties": {
            "job_id": {"type": "string"},
        }, "required": ["job_id"]}}},
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
        "description": "Surface a file (plot, table, report, dataset) into the chat as "
                       "an inline card the user can view and comment on for refinement. "
                       "Call this whenever you produce something the user should see.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the output file."},
            "caption": {"type": "string"},
            "kind": {"type": "string", "enum": ["image", "table", "report", "dataset", "text", "file"]},
            "provenance": {"type": "string", "description": "One line on how it was made."},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "record_provenance",
        "description": "Append a FAIR provenance entry (what was produced, from which "
                       "inputs, by which method) to science/PROVENANCE.md so every "
                       "result is reproducible.",
        "parameters": {"type": "object", "properties": {
            "artifact": {"type": "string"},
            "method": {"type": "string"},
            "inputs": {"type": "string"},
            "notes": {"type": "string"},
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
    "submit_cluster_job": _run_submit_job,
    "check_cluster_job": _run_check_job,
    "fetch_cluster_file": _run_fetch_cluster_file,
    "present_output": _run_present_output,
    "record_provenance": _run_record_provenance,
    "curate_dataset": _run_curate_dataset,
    "literature_search": _run_literature_search,
}


def register() -> None:
    """Register the science tools into the live dynamic-tool registry."""
    for d in SCIENCE_TOOL_DEFINITIONS:
        name = d["function"]["name"]
        _tools.register_dynamic_tool(d, _RUNNERS[name])


def unregister() -> None:
    for name in SCIENCE_TOOL_NAMES:
        _tools.unregister_dynamic_tool(name)


# The cluster-job subset — reused by the Lab (which does not want the rest of the
# science toolset). Backed by the thread-local compute context (set_compute_context).
CLUSTER_TOOL_NAMES = ("submit_cluster_job", "check_cluster_job", "fetch_cluster_file")


def register_cluster_tools() -> None:
    """Register ONLY the cluster-job tools into the live registry (Lab use)."""
    for d in SCIENCE_TOOL_DEFINITIONS:
        name = d["function"]["name"]
        if name in CLUSTER_TOOL_NAMES:
            _tools.register_dynamic_tool(d, _RUNNERS[name])


def unregister_cluster_tools() -> None:
    for name in CLUSTER_TOOL_NAMES:
        _tools.unregister_dynamic_tool(name)
