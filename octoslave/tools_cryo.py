"""
CryoUncle — CryoSPARC integration tools.

A companion toolbox for cryo-EM structural biologists. These tools let the
agent connect to a running CryoSPARC instance, browse projects / workspaces /
jobs, inspect result datasets (particles, micrographs), create and queue jobs,
and read job logs — so it can act as a hands-on bioinformatician driving the
single-particle workflow toward the best possible 3D structure.

Design mirrors ``tools_bio.py``:

* The heavy dependency (``cryosparc-tools``) is imported lazily. If it is
  missing, every tool returns a friendly "pip install cryosparc-tools" message
  instead of crashing.
* Connection credentials are collected interactively on first use (the agent
  calls ``cryo_connect`` after asking the user) and persisted to
  ``~/.octoslave/cryosparc.json`` (chmod 600). Subsequent tool calls reuse the
  stored connection automatically — the user only sets it up once.
* Every tool is defensive: CryoSPARC's Python API surface shifts between
  releases, so calls are wrapped and any AttributeError / connection failure is
  turned into an actionable message rather than an exception.

Nothing here is registered for other prompt profiles — ``tools.py`` only exposes
these schemas when ``profile == "cryouncle"``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

# ---------------------------------------------------------------------------
# Credential store  (~/.octoslave/cryosparc.json)
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".octoslave"
CRYO_CONFIG_FILE = CONFIG_DIR / "cryosparc.json"

# Fields we persist. `password` and `license` are secrets — the file is chmod
# 600. We deliberately keep them local (never sent to the LLM verbatim; tools
# report only non-secret status).
_CRED_FIELDS = ("license", "host", "base_port", "email", "password")


def _load_creds() -> dict:
    if not CRYO_CONFIG_FILE.exists():
        return {}
    try:
        with open(CRYO_CONFIG_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_creds(creds: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CRYO_CONFIG_FILE, "w") as f:
        json.dump(creds, f, indent=2)
    try:
        os.chmod(CRYO_CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        pass


def _redacted_creds() -> dict:
    """Non-secret view of stored credentials for status output."""
    c = _load_creds()
    if not c:
        return {}
    return {
        "host": c.get("host"),
        "base_port": c.get("base_port"),
        "email": c.get("email"),
        "license": _mask(c.get("license")),
        "password": "set" if c.get("password") else "unset",
    }


def _mask(value: str | None) -> str:
    if not value:
        return "unset"
    value = str(value)
    if len(value) <= 6:
        return "***"
    return f"{value[:4]}…{value[-2:]}"


# ---------------------------------------------------------------------------
# CryoSPARC client (lazy, cached)
# ---------------------------------------------------------------------------

_CLIENT = None            # cached CryoSPARC handle
_CLIENT_KEY = None        # creds tuple the cached client was built from


def _import_cryosparc():
    """Return the CryoSPARC class or None if the package isn't installed."""
    try:
        from cryosparc.tools import CryoSPARC  # type: ignore
        return CryoSPARC
    except Exception:
        return None


def _need_cryosparc() -> tuple[str, bool] | None:
    if _import_cryosparc() is None:
        return (
            "The `cryosparc-tools` package is not installed. Install it with:\n"
            "    pip install cryosparc-tools\n"
            "(or `uv pip install cryosparc-tools`). It is the official Python "
            "client for CryoSPARC and is required for every CryoUncle tool.",
            False,
        )
    return None


def _creds_key(c: dict) -> tuple:
    return tuple(c.get(k) for k in _CRED_FIELDS)


def _get_client(force_reconnect: bool = False):
    """Return (client, error_message). client is None on failure."""
    global _CLIENT, _CLIENT_KEY
    CryoSPARC = _import_cryosparc()
    if CryoSPARC is None:
        return None, ("The `cryosparc-tools` package is not installed. "
                      "Run: pip install cryosparc-tools")

    creds = _load_creds()
    missing = [k for k in _CRED_FIELDS if not creds.get(k) and k != "base_port"]
    if not creds:
        return None, (
            "Not connected to CryoSPARC yet. Ask the user for their CryoSPARC "
            "connection details (license ID, host, base port, email, password) "
            "and call `cryo_connect` to set it up — this is a one-time step.")
    if missing:
        return None, (
            f"CryoSPARC credentials are incomplete (missing: {', '.join(missing)}). "
            "Ask the user for the missing values and call `cryo_connect` again.")

    key = _creds_key(creds)
    if _CLIENT is not None and _CLIENT_KEY == key and not force_reconnect:
        return _CLIENT, None

    try:
        cs = CryoSPARC(
            license=creds["license"],
            host=creds["host"],
            base_port=int(creds.get("base_port") or 39000),
            email=creds["email"],
            password=creds["password"],
        )
        # Best-effort connection check (method name is stable across releases).
        ok = True
        try:
            ok = bool(cs.test_connection())
        except Exception:
            ok = True  # some versions omit test_connection; assume ok
        if not ok:
            return None, (
                "Connected to the CryoSPARC host but the connection test failed. "
                "Double-check the license ID, email and password with the user.")
        _CLIENT = cs
        _CLIENT_KEY = key
        return cs, None
    except Exception as e:
        return None, (
            f"Could not connect to CryoSPARC: {e}\n"
            "Verify with the user that the host/port are reachable and the "
            "license/email/password are correct, then call `cryo_connect` again.")


# ---------------------------------------------------------------------------
# Tool schemas (sent to the model)
# ---------------------------------------------------------------------------

CRYO_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "cryo_connect",
            "description": (
                "Set up (or update) the connection to the user's CryoSPARC "
                "instance. Call this ONCE during first-time setup, after asking "
                "the user for their details via ask_user. Persists the "
                "credentials locally (~/.octoslave/cryosparc.json, chmod 600) and "
                "verifies the connection. Any field you omit is kept from the "
                "previously stored value, so you can update just the password. "
                "After a successful connect, all other cryo_* tools work "
                "automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "license": {"type": "string", "description": "CryoSPARC license ID (UUID form)"},
                    "host": {"type": "string", "description": "Hostname/IP of the CryoSPARC master (e.g. 'localhost' or 'cryo.lab.edu')"},
                    "base_port": {"type": "integer", "description": "CryoSPARC base port (default 39000)"},
                    "email": {"type": "string", "description": "CryoSPARC account email (login)"},
                    "password": {"type": "string", "description": "CryoSPARC account password"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_status",
            "description": (
                "Report the current CryoSPARC connection status: which host/email "
                "is configured (secrets masked), whether the client can reach the "
                "instance, and CryoSPARC version/scheduler info when available. "
                "Use this to confirm setup or diagnose connection problems."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_projects",
            "description": (
                "List projects on the connected CryoSPARC instance: project UID, "
                "title, owner, size, and directory. Start here when browsing the "
                "user's data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max projects to list (default 50)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_workspaces",
            "description": (
                "List workspaces inside a CryoSPARC project (UID, title, job "
                "count). Workspaces organise the jobs of a processing session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                },
                "required": ["project_uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_jobs",
            "description": (
                "List jobs in a project (optionally scoped to one workspace). "
                "Returns job UID, type, title, status and creation order. Filter "
                "by status ('completed', 'running', 'failed', 'queued', "
                "'building') or by job type substring to zero in on relevant "
                "steps of the pipeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                    "workspace_uid": {"type": "string", "description": "Optional workspace UID, e.g. 'W2'"},
                    "status": {"type": "string", "description": "Optional status filter (completed/running/failed/queued/building/killed)"},
                    "type_contains": {"type": "string", "description": "Optional case-insensitive substring to match the job type (e.g. 'refine', 'class', 'ctf')"},
                    "limit": {"type": "integer", "description": "Max jobs to list (default 60)"},
                },
                "required": ["project_uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_job",
            "description": (
                "Full detail for one job: type, status, title/description, "
                "parameters (non-default highlighted), input connections, output "
                "result groups, and the tail of its event log. Use this to "
                "understand what a step did and what it produced before deciding "
                "the next experiment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                    "job_uid": {"type": "string", "description": "Job UID, e.g. 'J42'"},
                    "log_lines": {"type": "integer", "description": "Lines of event log to include (default 40, max 200)"},
                },
                "required": ["project_uid", "job_uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_job_types",
            "description": (
                "List the job/builder types available on this CryoSPARC instance "
                "(e.g. import_movies, patch_motion_correction, patch_ctf_estimation, "
                "blob_picker, extract_micrographs, class_2D, select_2D, "
                "homogeneous_refine, nonuniform_refine, ab_initio). Use this to "
                "find the exact `type` string to pass to cryo_create_job."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contains": {"type": "string", "description": "Optional case-insensitive substring filter"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_dataset",
            "description": (
                "Inspect an output dataset of a completed job (e.g. particles, "
                "micrographs, volumes). Returns the row count and the available "
                "fields/slots plus a small preview and numeric summary of key "
                "columns — the cryo-EM equivalent of bio_inspect. Use this to "
                "check particle counts, resolution/CTF fit columns, defocus "
                "spread, pose distributions, etc., before suggesting next steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                    "job_uid": {"type": "string", "description": "Job UID, e.g. 'J42'"},
                    "output": {"type": "string", "description": "Output result-group name (e.g. 'particles', 'micrographs', 'volume'). If omitted, the first output is used."},
                    "fields": {"type": "array", "items": {"type": "string"}, "description": "Optional subset of fields/slots to load (faster). If omitted, a light default set is loaded."},
                },
                "required": ["project_uid", "job_uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_create_job",
            "description": (
                "Create a new CryoSPARC job in a workspace. Specify the job type "
                "(see cryo_job_types), any non-default parameters, and input "
                "connections wiring outputs of earlier jobs to this job's inputs. "
                "By default the job is only BUILT (not run) so the user can "
                "review it; set queue=true (with a lane) to also submit it. "
                "MUTATING: this changes the user's CryoSPARC project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                    "workspace_uid": {"type": "string", "description": "Workspace UID, e.g. 'W2'"},
                    "type": {"type": "string", "description": "Job/builder type, e.g. 'homogeneous_refine'"},
                    "params": {"type": "object", "description": "Parameter overrides as a JSON object, e.g. {\"refine_symmetry\": \"C2\"}"},
                    "connections": {"type": "object", "description": "Input wiring: {input_name: [\"Jxx.output_name\", ...]} or {input_name: \"Jxx.output_name\"}"},
                    "title": {"type": "string", "description": "Optional job title"},
                    "desc": {"type": "string", "description": "Optional job description / rationale"},
                    "queue": {"type": "boolean", "description": "If true, queue the job after building (default false)"},
                    "lane": {"type": "string", "description": "Scheduler lane to queue on (required if queue=true)"},
                },
                "required": ["project_uid", "workspace_uid", "type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_queue_job",
            "description": (
                "Queue (submit to the scheduler) an already-built job so it runs. "
                "Choose the compute lane; optionally pin a hostname or GPU count. "
                "MUTATING: this launches computation on the user's cluster."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                    "job_uid": {"type": "string", "description": "Job UID, e.g. 'J42'"},
                    "lane": {"type": "string", "description": "Scheduler lane name (see cryo_status for available lanes)"},
                    "hostname": {"type": "string", "description": "Optional specific worker hostname"},
                    "gpus": {"type": "integer", "description": "Optional number of GPUs to request"},
                },
                "required": ["project_uid", "job_uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_control_job",
            "description": (
                "Control a job's lifecycle: 'kill' a running/queued job, 'clear' "
                "it back to building, or 'clone' it into a new job (optionally "
                "into another workspace). MUTATING."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                    "job_uid": {"type": "string", "description": "Job UID, e.g. 'J42'"},
                    "action": {"type": "string", "description": "One of: kill, clear, clone"},
                    "workspace_uid": {"type": "string", "description": "For clone: target workspace UID (default same workspace)"},
                },
                "required": ["project_uid", "job_uid", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cryo_download",
            "description": (
                "Download a file produced by a job (or any path relative to the "
                "project directory) to the local working directory — e.g. a "
                "refined map (.mrc), an FSC curve, or a report. Use to bring "
                "results local for further analysis or to include in a report."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_uid": {"type": "string", "description": "Project UID, e.g. 'P3'"},
                    "path": {"type": "string", "description": "Path relative to the project directory (e.g. 'J42/J42_005_volume_map_sharp.mrc')"},
                    "output_path": {"type": "string", "description": "Optional local destination filename (defaults to the basename in the working dir)"},
                },
                "required": ["project_uid", "path"],
            },
        },
    },
]

CRYO_TOOL_NAMES = frozenset(t["function"]["name"] for t in CRYO_TOOL_DEFINITIONS)

# Cryo tools that change the CryoSPARC project/cluster state — gated like other
# modifying tools when the run is in a permission-controlled mode.
CRYO_MODIFYING_TOOLS = frozenset({
    "cryo_create_job", "cryo_queue_job", "cryo_control_job",
})


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def execute_cryo_tool(name: str, args: dict, working_dir: str) -> tuple[str, bool]:
    """Returns (result_text, success). Returns (None, None) if `name` isn't a cryo tool."""
    if name not in CRYO_TOOL_NAMES:
        return None, None
    try:
        if name == "cryo_connect":
            return _cryo_connect(**args)
        if name == "cryo_status":
            return _cryo_status()
        if name == "cryo_projects":
            return _cryo_projects(**args)
        if name == "cryo_workspaces":
            return _cryo_workspaces(**args)
        if name == "cryo_jobs":
            return _cryo_jobs(**args)
        if name == "cryo_job":
            return _cryo_job(**args)
        if name == "cryo_job_types":
            return _cryo_job_types(**args)
        if name == "cryo_dataset":
            return _cryo_dataset(**args)
        if name == "cryo_create_job":
            return _cryo_create_job(working_dir=working_dir, **args)
        if name == "cryo_queue_job":
            return _cryo_queue_job(**args)
        if name == "cryo_control_job":
            return _cryo_control_job(**args)
        if name == "cryo_download":
            return _cryo_download(working_dir=working_dir, **args)
    except TypeError as e:
        return f"Invalid arguments for {name}: {e}", False
    except Exception as e:
        return f"Tool error ({name}): {e}", False
    return f"Unknown cryo tool: {name}", False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(obj):
    """Best-effort extraction of a CryoSPARC document dict from a wrapper object."""
    for attr in ("doc", "_doc", "data"):
        d = getattr(obj, attr, None)
        if isinstance(d, dict):
            return d
    if isinstance(obj, dict):
        return obj
    return {}


def _fmt_kv(d: dict, keys) -> str:
    parts = []
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            parts.append(f"{k}={d[k]}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# cryo_connect / cryo_status
# ---------------------------------------------------------------------------

def _cryo_connect(license: str = None, host: str = None, base_port: int = None,
                  email: str = None, password: str = None) -> tuple[str, bool]:
    dep = _need_cryosparc()
    if dep is not None:
        # Still persist what we were given so setup survives the install.
        pass

    existing = _load_creds()
    creds = dict(existing)
    if license is not None:
        creds["license"] = str(license).strip()
    if host is not None:
        creds["host"] = str(host).strip()
    if base_port is not None:
        creds["base_port"] = int(base_port)
    elif "base_port" not in creds:
        creds["base_port"] = 39000
    if email is not None:
        creds["email"] = str(email).strip()
    if password is not None:
        creds["password"] = str(password)

    # Persist whatever we have so interactive setup can proceed one field at a
    # time (e.g. host now, password on the next call).
    _save_creds(creds)

    required = ("license", "host", "email", "password")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        return (
            f"Saved partial CryoSPARC settings, but still missing: "
            f"{', '.join(missing)}. Ask the user for these and call cryo_connect "
            f"again. (Stored so far: host={creds.get('host')}, "
            f"port={creds.get('base_port')}, email={creds.get('email')}.)",
            True,
        )

    if dep is not None:
        return (
            "Credentials saved to ~/.octoslave/cryosparc.json, but "
            "`cryosparc-tools` is not installed so the connection can't be "
            "verified yet. Install it with `pip install cryosparc-tools`, then "
            "call cryo_status to confirm.",
            True,
        )

    # Force a fresh connect with the new creds.
    client, err = _get_client(force_reconnect=True)
    if client is None:
        return (
            f"Credentials saved, but the connection test failed:\n{err}",
            False,
        )
    info = _instance_info(client)
    return (
        "✓ Connected to CryoSPARC and saved the connection.\n"
        f"  host={creds['host']}:{creds['base_port']}  email={creds['email']}\n"
        f"{info}\n"
        "Setup complete — you can now browse projects (cryo_projects), inspect "
        "jobs and datasets, and create/queue jobs.",
        True,
    )


def _instance_info(client) -> str:
    """Best-effort version + lane summary for status output."""
    lines = []
    # version
    ver = None
    for getter in ("get_version", "version"):
        try:
            v = getattr(client, getter, None)
            ver = v() if callable(v) else v
            if ver:
                break
        except Exception:
            ver = None
    if ver:
        lines.append(f"  CryoSPARC version: {ver}")
    # lanes / scheduler targets
    try:
        targets = None
        for getter in ("get_scheduler_lanes", "get_lanes"):
            fn = getattr(client, getter, None)
            if callable(fn):
                targets = fn()
                break
        if targets:
            names = []
            for t in targets:
                d = _doc(t)
                names.append(d.get("name") or d.get("title") or str(t))
            if names:
                lines.append(f"  Scheduler lanes: {', '.join(names[:12])}")
    except Exception:
        pass
    return "\n".join(lines) if lines else "  (version/lane info unavailable on this release)"


def _cryo_status() -> tuple[str, bool]:
    red = _redacted_creds()
    if not red:
        return (
            "No CryoSPARC connection is configured yet. Ask the user for their "
            "license ID, host, base port, email and password, then call "
            "cryo_connect. This one-time setup is all it takes.",
            True,
        )
    dep = _need_cryosparc()
    header = (
        "CryoSPARC connection (stored in ~/.octoslave/cryosparc.json):\n"
        f"  host={red['host']}:{red['base_port']}  email={red['email']}  "
        f"license={red['license']}  password={red['password']}"
    )
    if dep is not None:
        return header + "\n\n" + dep[0], True

    client, err = _get_client()
    if client is None:
        return header + f"\n\n✗ Not reachable: {err}", True
    return header + "\n✓ Reachable.\n" + _instance_info(client), True


# ---------------------------------------------------------------------------
# Browse: projects / workspaces / jobs
# ---------------------------------------------------------------------------

def _cryo_projects(limit: int = 50) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    try:
        projects = list(client.find_projects())
    except AttributeError:
        try:
            projects = list(client.cli.list_projects())  # type: ignore
        except Exception as e:
            return f"Could not list projects on this CryoSPARC release: {e}", False
    if not projects:
        return "No projects found on this CryoSPARC instance.", True
    lines = [f"Projects ({min(len(projects), limit)} of {len(projects)}):"]
    for p in projects[:limit]:
        d = _doc(p)
        uid = d.get("uid") or getattr(p, "uid", "?")
        title = d.get("title", "")
        owner = d.get("owner_user_id") or d.get("created_by_user_id") or ""
        pdir = d.get("project_dir") or d.get("directory") or ""
        lines.append(f"  {uid}: {title}  [owner={owner}]  {pdir}")
    return "\n".join(lines), True


def _cryo_workspaces(project_uid: str) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    try:
        wss = list(client.find_workspaces(project_uid))
    except AttributeError:
        try:
            proj = client.find_project(project_uid)
            wss = list(proj.find_workspaces())
        except Exception as e:
            return f"Could not list workspaces for {project_uid}: {e}", False
    if not wss:
        return f"No workspaces in {project_uid}.", True
    lines = [f"Workspaces in {project_uid}:"]
    for w in wss:
        d = _doc(w)
        uid = d.get("uid") or getattr(w, "uid", "?")
        title = d.get("title", "")
        n = d.get("job_count") or d.get("workspace_job_count") or ""
        lines.append(f"  {uid}: {title}  [jobs={n}]")
    return "\n".join(lines), True


def _cryo_jobs(project_uid: str, workspace_uid: str = None, status: str = None,
               type_contains: str = None, limit: int = 60) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    try:
        if workspace_uid:
            jobs = list(client.find_jobs(project_uid, workspace_uid))
        else:
            jobs = list(client.find_jobs(project_uid))
    except AttributeError:
        try:
            proj = client.find_project(project_uid)
            jobs = list(proj.find_jobs())
        except Exception as e:
            return f"Could not list jobs for {project_uid}: {e}", False
    except Exception as e:
        return f"Could not list jobs for {project_uid}: {e}", False

    rows = []
    for j in jobs:
        d = _doc(j)
        rows.append({
            "uid": d.get("uid") or getattr(j, "uid", "?"),
            "type": d.get("type") or d.get("job_type") or "",
            "status": d.get("status", ""),
            "title": d.get("title", ""),
            "ws": d.get("workspace_uids") or d.get("workspace_uid") or "",
        })
    if status:
        rows = [r for r in rows if str(r["status"]).lower() == status.lower()]
    if type_contains:
        tc = type_contains.lower()
        rows = [r for r in rows if tc in str(r["type"]).lower()]
    if not rows:
        return (f"No jobs in {project_uid}"
                + (f"/{workspace_uid}" if workspace_uid else "")
                + " match the filter.", True)
    total = len(rows)
    header = (f"Jobs in {project_uid}"
              + (f"/{workspace_uid}" if workspace_uid else "")
              + f" ({min(total, limit)} of {total}):")
    lines = [header]
    for r in rows[:limit]:
        lines.append(f"  {r['uid']:>5}  {str(r['status']):<10} {str(r['type']):<28} {r['title']}")
    return "\n".join(lines), True


def _get_job(client, project_uid: str, job_uid: str):
    try:
        return client.find_job(project_uid, job_uid)
    except Exception:
        proj = client.find_project(project_uid)
        return proj.find_job(job_uid)


def _cryo_job(project_uid: str, job_uid: str, log_lines: int = 40) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    log_lines = max(1, min(int(log_lines or 40), 200))
    try:
        job = _get_job(client, project_uid, job_uid)
    except Exception as e:
        return f"Could not load {project_uid}/{job_uid}: {e}", False

    d = _doc(job)
    lines = [f"Job {project_uid}/{job_uid}"]
    lines.append("  " + _fmt_kv(d, ("type", "status", "title")))
    desc = d.get("description")
    if desc:
        lines.append(f"  description: {str(desc)[:400]}")

    # Non-default params
    params = d.get("params_spec") or d.get("params") or {}
    if isinstance(params, dict) and params:
        shown = []
        for k, v in list(params.items())[:30]:
            val = v.get("value") if isinstance(v, dict) else v
            shown.append(f"{k}={val}")
        lines.append("  params: " + ", ".join(shown))

    # Inputs
    inputs = d.get("input_slot_groups") or d.get("inputs") or []
    if inputs:
        names = []
        for g in inputs:
            gd = g if isinstance(g, dict) else _doc(g)
            nm = gd.get("name") or gd.get("type") or ""
            conns = gd.get("connections") or []
            src = ", ".join(
                f"{c.get('job_uid','?')}.{c.get('group_name', c.get('slot_name',''))}"
                for c in conns if isinstance(c, dict)
            )
            names.append(f"{nm}<-[{src}]" if src else nm)
        lines.append("  inputs: " + "; ".join(n for n in names if n))

    # Outputs
    outs = d.get("output_result_groups") or d.get("outputs") or []
    if outs:
        onames = []
        for g in outs:
            gd = g if isinstance(g, dict) else _doc(g)
            nm = gd.get("name") or gd.get("type") or ""
            n = gd.get("num_items") or gd.get("count")
            onames.append(f"{nm}({n})" if n is not None else nm)
        lines.append("  outputs: " + ", ".join(o for o in onames if o))

    # Event log tail
    log_text = _job_log_tail(job, log_lines)
    if log_text:
        lines.append(f"  --- last {log_lines} log lines ---")
        lines.append(log_text)
    return "\n".join(lines), True


def _job_log_tail(job, n: int) -> str:
    for getter in ("get_event_logs", "get_job_log", "log"):
        fn = getattr(job, getter, None)
        if not callable(fn):
            continue
        try:
            out = fn()
        except Exception:
            continue
        if out is None:
            continue
        if isinstance(out, (list, tuple)):
            text_lines = []
            for e in out:
                if isinstance(e, dict):
                    text_lines.append(str(e.get("text") or e.get("message") or e))
                else:
                    text_lines.append(str(e))
            return "\n".join(text_lines[-n:])
        text = str(out)
        return "\n".join(text.splitlines()[-n:])
    return ""


def _cryo_job_types(contains: str = None) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    specs = None
    for getter in ("get_job_specs", "get_job_sections", "list_job_types"):
        fn = getattr(client, getter, None)
        if callable(fn):
            try:
                specs = fn()
                break
            except Exception:
                specs = None
    types: list[str] = []
    if isinstance(specs, dict):
        # get_job_specs -> {category: {type: spec}} or {type: spec}
        for k, v in specs.items():
            if isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()):
                types.extend(v.keys())
            else:
                types.append(k)
    elif isinstance(specs, (list, tuple)):
        for s in specs:
            d = s if isinstance(s, dict) else _doc(s)
            t = d.get("type") or d.get("job_type") or d.get("name")
            if t:
                types.append(t)
    if not types:
        return (
            "Could not enumerate job types from this CryoSPARC release. Common "
            "types you can pass to cryo_create_job: import_movies, "
            "import_micrographs, patch_motion_correction, patch_ctf_estimation, "
            "curate_exposures, blob_picker, template_picker, extract_micrographs, "
            "class_2D, select_2D, ab_initio, homogeneous_refine, "
            "nonuniform_refine, homogeneous_reconstruct, local_refine, "
            "3D_classification, sharpen, validation.",
            True,
        )
    types = sorted(set(types))
    if contains:
        c = contains.lower()
        types = [t for t in types if c in t.lower()]
    return f"Available job types ({len(types)}):\n  " + "\n  ".join(types), True


# ---------------------------------------------------------------------------
# Inspect datasets
# ---------------------------------------------------------------------------

def _cryo_dataset(project_uid: str, job_uid: str, output: str = None,
                  fields: list = None) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    try:
        job = _get_job(client, project_uid, job_uid)
    except Exception as e:
        return f"Could not load {project_uid}/{job_uid}: {e}", False

    # Discover output names if not specified
    d = _doc(job)
    outs = d.get("output_result_groups") or d.get("outputs") or []
    out_names = []
    for g in outs:
        gd = g if isinstance(g, dict) else _doc(g)
        nm = gd.get("name")
        if nm:
            out_names.append(nm)
    if output is None:
        if not out_names:
            return (f"Job {job_uid} has no listed outputs to inspect. Check it "
                    f"is completed (cryo_job {project_uid} {job_uid}).", True)
        output = out_names[0]

    try:
        if fields:
            dset = job.load_output(output, slots=list(fields))
        else:
            dset = job.load_output(output)
    except TypeError:
        dset = job.load_output(output)
    except Exception as e:
        avail = f" Available outputs: {', '.join(out_names)}." if out_names else ""
        return (f"Could not load output '{output}' of {job_uid}: {e}.{avail}", False)

    return _summarize_dataset(dset, f"{project_uid}/{job_uid}:{output}"), True


def _summarize_dataset(dset, label: str) -> str:
    lines = [f"Dataset {label}"]
    # row count
    n = None
    try:
        n = len(dset)
    except Exception:
        n = getattr(dset, "nrow", None)
    lines.append(f"  rows: {n}")
    # fields
    fields = []
    try:
        fields = list(dset.fields())
    except Exception:
        try:
            fields = list(dset.descr())
        except Exception:
            fields = []
    if fields:
        flat = []
        for f in fields:
            if isinstance(f, (list, tuple)):
                flat.append(str(f[0]))
            else:
                flat.append(str(f))
        lines.append(f"  fields ({len(flat)}): " + ", ".join(flat[:60])
                     + (" …" if len(flat) > 60 else ""))
    # numeric summary of a few interesting columns
    interesting = [f for f in _flatten_field_names(fields)
                   if any(k in f.lower() for k in
                          ("ctf", "df", "defocus", "resolution", "res_",
                           "error", "score", "shift", "phase", "pose"))]
    shown = 0
    for col in interesting:
        try:
            import numpy as np  # noqa
            arr = np.asarray(dset[col])
            if arr.dtype.kind in "fiu" and arr.size:
                lines.append(
                    f"    {col}: min={float(arr.min()):.3g} "
                    f"mean={float(arr.mean()):.3g} max={float(arr.max()):.3g}")
                shown += 1
        except Exception:
            continue
        if shown >= 10:
            break
    return "\n".join(lines)


def _flatten_field_names(fields) -> list[str]:
    out = []
    for f in fields:
        if isinstance(f, (list, tuple)):
            out.append(str(f[0]))
        else:
            out.append(str(f))
    return out


# ---------------------------------------------------------------------------
# Create / queue / control jobs
# ---------------------------------------------------------------------------

def _parse_connections(connections: dict) -> dict:
    """Normalise {input: 'Jxx.output'} or {input: ['Jxx.output', ...]} into the
    tuple form CryoSPARC create_job expects: {input: [(job_uid, output), ...]}."""
    parsed = {}
    for inp, spec in (connections or {}).items():
        items = spec if isinstance(spec, (list, tuple)) else [spec]
        pairs = []
        for it in items:
            if isinstance(it, (list, tuple)) and len(it) == 2:
                pairs.append((str(it[0]), str(it[1])))
            elif isinstance(it, str) and "." in it:
                juid, out = it.split(".", 1)
                pairs.append((juid.strip(), out.strip()))
            else:
                pairs.append(it)
        parsed[inp] = pairs
    return parsed


def _cryo_create_job(project_uid: str, workspace_uid: str, type: str,
                     working_dir: str, params: dict = None, connections: dict = None,
                     title: str = None, desc: str = None, queue: bool = False,
                     lane: str = None) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    conns = _parse_connections(connections or {})
    kwargs = {}
    if params:
        kwargs["params"] = params
    if conns:
        kwargs["connections"] = conns
    if title:
        kwargs["title"] = title
    if desc:
        kwargs["desc"] = desc
    try:
        job = client.create_job(project_uid, workspace_uid, type, **kwargs)
    except TypeError:
        # Older signature: create_job(project, workspace, type, params, connections)
        try:
            job = client.create_job(project_uid, workspace_uid, type,
                                    params or {}, conns or {})
        except Exception as e:
            return f"Could not create job of type '{type}': {e}", False
    except Exception as e:
        return (f"Could not create job of type '{type}': {e}. "
                f"Check the type with cryo_job_types and the input names with "
                f"cryo_job on a source job.", False)

    juid = _doc(job).get("uid") or getattr(job, "uid", "?")
    msg = [f"✓ Built job {juid} (type={type}) in {project_uid}/{workspace_uid}."]
    if params:
        msg.append(f"  params: {json.dumps(params)[:300]}")
    if conns:
        msg.append(f"  connections: {list(conns.keys())}")

    if queue:
        if not lane:
            msg.append("  NOT queued: `lane` is required to queue. Call "
                       "cryo_queue_job with a lane, or re-run with lane set.")
            return "\n".join(msg), True
        ok, qmsg = _do_queue(job, lane, None, None)
        msg.append("  " + qmsg)
        return "\n".join(msg), ok
    msg.append("  Built only (not queued). Review it, then call cryo_queue_job "
               "to run it, or tell the user it's ready.")
    return "\n".join(msg), True


def _do_queue(job, lane: str, hostname: str, gpus: int) -> tuple[bool, str]:
    kwargs = {}
    if lane:
        kwargs["lane"] = lane
    if hostname:
        kwargs["hostname"] = hostname
    if gpus:
        kwargs["num_gpus"] = gpus
    try:
        job.queue(**kwargs)
        return True, f"Queued on lane '{lane}'."
    except TypeError:
        try:
            job.queue(lane)
            return True, f"Queued on lane '{lane}'."
        except Exception as e:
            return False, f"Queue failed: {e}"
    except Exception as e:
        return False, f"Queue failed: {e}"


def _cryo_queue_job(project_uid: str, job_uid: str, lane: str = None,
                    hostname: str = None, gpus: int = None) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    try:
        job = _get_job(client, project_uid, job_uid)
    except Exception as e:
        return f"Could not load {project_uid}/{job_uid}: {e}", False
    ok, msg = _do_queue(job, lane, hostname, gpus)
    prefix = "✓" if ok else "✗"
    return f"{prefix} {project_uid}/{job_uid}: {msg}", ok


def _cryo_control_job(project_uid: str, job_uid: str, action: str,
                      workspace_uid: str = None) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    action = (action or "").lower().strip()
    try:
        job = _get_job(client, project_uid, job_uid)
    except Exception as e:
        return f"Could not load {project_uid}/{job_uid}: {e}", False

    try:
        if action == "kill":
            job.kill()
            return f"✓ Killed {project_uid}/{job_uid}.", True
        if action == "clear":
            job.clear()
            return f"✓ Cleared {project_uid}/{job_uid} back to building.", True
        if action == "clone":
            try:
                new = job.clone(workspace_uid) if workspace_uid else job.clone()
            except TypeError:
                new = job.clone()
            nuid = _doc(new).get("uid") or getattr(new, "uid", "?")
            return f"✓ Cloned {project_uid}/{job_uid} → {nuid}.", True
    except Exception as e:
        return f"Action '{action}' failed on {job_uid}: {e}", False
    return f"Unknown action '{action}'. Use one of: kill, clear, clone.", False


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _cryo_download(project_uid: str, path: str, working_dir: str,
                   output_path: str = None) -> tuple[str, bool]:
    client, err = _get_client()
    if client is None:
        return err, False
    dest = Path(output_path) if output_path else Path(working_dir) / Path(path).name
    if not dest.is_absolute():
        dest = Path(working_dir) / dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    proj = None
    try:
        proj = client.find_project(project_uid)
    except Exception:
        proj = None

    # Try project.download_file, then client.download_file.
    for holder, name in ((proj, "project"), (client, "client")):
        if holder is None:
            continue
        fn = getattr(holder, "download_file", None)
        if not callable(fn):
            continue
        try:
            try:
                fn(path, str(dest))
            except TypeError:
                fn(project_uid, path, str(dest))
            if dest.exists():
                size = dest.stat().st_size
                return (f"✓ Downloaded {project_uid}:{path} → {dest} "
                        f"({size/1e6:.2f} MB).", True)
        except Exception as e:
            last = e
            continue
    return (f"Could not download {project_uid}:{path}. Verify the path is "
            f"relative to the project directory (see cryo_job outputs).", False)
