"""Persistent state for a Science session (the orchestrator's blackboard).

Everything a run produces — the conversation, the specialists that were spun up,
cluster jobs, presented artifacts (plots/tables/reports) with their comment
threads, and a FAIR provenance ledger — is persisted under
``<working_dir>/science/state.json`` so a session can be reloaded and continued.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


@dataclass
class Specialist:
    """A focused agent the orchestrator spun up for one task."""
    name: str
    role: str
    goal: str
    tools: list[str] = field(default_factory=list)
    icon: str = "🔬"
    status: str = "idle"          # idle | working | done | failed
    summary: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Job:
    """A job submitted to a remote cluster (or run in the background locally)."""
    name: str
    command: str
    remote_id: Optional[str] = None
    remote_label: str = "local"
    scheduler: str = "shell"      # shell | slurm | pbs
    handle: str = ""              # PID or scheduler job id
    cwd: str = ""
    status: str = "submitted"     # submitted | running | done | failed | unknown
    output: str = ""
    submitted_at: str = field(default_factory=_now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Artifact:
    """An output surfaced into the chat for viewing / commenting / refinement."""
    path: str                     # absolute path
    rel: str = ""                 # path relative to working_dir (for the viewer)
    caption: str = ""
    kind: str = "file"            # image | table | report | dataset | text | file
    created_at: str = field(default_factory=_now)
    comments: list[dict] = field(default_factory=list)   # {text, at}
    provenance: str = ""          # short "how this was made" note
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


class ScienceSession:
    """Mutable, persisted state for one Science session."""

    def __init__(self, task: str, working_dir: str, model: str = ""):
        self.task = task
        self.working_dir = str(Path(working_dir).expanduser().resolve())
        self.model = model
        self.created_at = _now()
        self.updated_at = self.created_at
        # OpenAI-style message history for the orchestrator (system+user+assistant+tool).
        self.messages: list[dict] = []
        self.specialists: list[Specialist] = []
        self.jobs: list[Job] = []
        self.artifacts: list[Artifact] = []
        self.provenance: list[dict] = []   # {artifact, method, inputs, notes, at}
        self.remote_id: Optional[str] = None
        self._lock = threading.Lock()

    # -- filesystem layout ------------------------------------------------
    @property
    def science_dir(self) -> Path:
        return Path(self.working_dir) / "science"

    @property
    def state_path(self) -> Path:
        return self.science_dir / "state.json"

    @property
    def provenance_path(self) -> Path:
        return self.science_dir / "PROVENANCE.md"

    # -- mutation helpers -------------------------------------------------
    def touch(self) -> None:
        self.updated_at = _now()

    def add_specialist(self, spec: Specialist) -> Specialist:
        with self._lock:
            self.specialists.append(spec)
            self.touch()
            self._save_locked()
        return spec

    def update_specialist(self, sid: str, **fields) -> None:
        with self._lock:
            for s in self.specialists:
                if s.id == sid:
                    for k, v in fields.items():
                        setattr(s, k, v)
                    break
            self.touch()
            self._save_locked()

    def add_job(self, job: Job) -> Job:
        with self._lock:
            self.jobs.append(job)
            self.touch()
            self._save_locked()
        return job

    def get_job(self, jid: str) -> Optional[Job]:
        for j in self.jobs:
            if j.id == jid or j.handle == jid or j.name == jid:
                return j
        return None

    def add_artifact(self, art: Artifact) -> Artifact:
        with self._lock:
            # de-dupe by path: update the existing card rather than stacking copies
            for a in self.artifacts:
                if a.path == art.path:
                    a.caption = art.caption or a.caption
                    a.kind = art.kind or a.kind
                    a.provenance = art.provenance or a.provenance
                    a.created_at = _now()
                    self.touch()
                    self._save_locked()
                    return a
            self.artifacts.append(art)
            self.touch()
            self._save_locked()
        return art

    def get_artifact(self, aid: str) -> Optional[Artifact]:
        for a in self.artifacts:
            if a.id == aid:
                return a
        return None

    def comment_artifact(self, aid: str, text: str) -> Optional[Artifact]:
        with self._lock:
            for a in self.artifacts:
                if a.id == aid:
                    a.comments.append({"text": text, "at": _now()})
                    self.touch()
                    self._save_locked()
                    return a
        return None

    def add_provenance(self, entry: dict) -> None:
        with self._lock:
            entry = {"at": _now(), **entry}
            self.provenance.append(entry)
            self.touch()
            self._save_locked()
            self._write_provenance_md_locked()

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "working_dir": self.working_dir,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": self.messages,
            "specialists": [asdict(s) for s in self.specialists],
            "jobs": [asdict(j) for j in self.jobs],
            "artifacts": [asdict(a) for a in self.artifacts],
            "provenance": self.provenance,
            "remote_id": self.remote_id,
        }

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.science_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        os.replace(tmp, self.state_path)

    def _write_provenance_md_locked(self) -> None:
        lines = [
            "# Provenance & Reproducibility Ledger",
            "",
            f"_Session started {self.created_at} — working dir `{self.working_dir}`._",
            "",
            "FAIR record of every derived output: what was produced, from which",
            "inputs, by which method. Regenerating any artifact should require only",
            "the inputs and method named below.",
            "",
        ]
        for e in self.provenance:
            lines.append(f"## {e.get('artifact', '(unnamed output)')}")
            lines.append(f"- **when:** {e.get('at', '')}")
            if e.get("method"):
                lines.append(f"- **method:** {e['method']}")
            if e.get("inputs"):
                lines.append(f"- **inputs:** {e['inputs']}")
            if e.get("notes"):
                lines.append(f"- **notes:** {e['notes']}")
            lines.append("")
        self.provenance_path.write_text("\n".join(lines))

    @classmethod
    def load(cls, working_dir: str) -> Optional["ScienceSession"]:
        p = Path(working_dir).expanduser().resolve() / "science" / "state.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except Exception:
            return None
        s = cls(task=data.get("task", ""), working_dir=working_dir,
                model=data.get("model", ""))
        s.created_at = data.get("created_at", s.created_at)
        s.updated_at = data.get("updated_at", s.updated_at)
        s.messages = data.get("messages", [])
        s.specialists = [Specialist(**_known(Specialist, d))
                         for d in data.get("specialists", [])]
        s.jobs = [Job(**_known(Job, d)) for d in data.get("jobs", [])]
        s.artifacts = [Artifact(**_known(Artifact, d)) for d in data.get("artifacts", [])]
        s.provenance = data.get("provenance", [])
        s.remote_id = data.get("remote_id")
        return s

    # -- UI snapshot ------------------------------------------------------
    def snapshot(self) -> dict:
        """Everything the web UI needs to render/rehydrate the session."""
        return {
            "task": self.task,
            "working_dir": self.working_dir,
            "specialists": [asdict(s) for s in self.specialists],
            "jobs": [asdict(j) for j in self.jobs],
            "artifacts": [asdict(a) for a in self.artifacts],
            "provenance": self.provenance,
        }


def _known(cls, d: dict) -> dict:
    fields = set(getattr(cls, "__dataclass_fields__", {}))
    return {k: v for k, v in d.items() if k in fields}
