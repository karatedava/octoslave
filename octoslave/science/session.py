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
    # An early look (a first-pass plot while the full run continues) rather than
    # a finished result — shown with a "preview" badge until re-presented final.
    interim: bool = False
    # Who presented it: "" = the orchestrator, else the specialist's name.
    by: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


class ScienceSession:
    """Mutable, persisted state for one Science session."""

    def __init__(self, task: str, working_dir: str, model: str = ""):
        self.task = task
        self.working_dir = str(Path(working_dir).expanduser().resolve())
        # The models CHOSEN for this session in the UI. Persisted so reopening a
        # session (or a refresh, which skips the start form) keeps running on what
        # the user picked instead of silently reverting to config's default_model.
        self.model = model
        self.specialist_models: list[str] = []
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

    def get_specialist(self, ref: str) -> Optional[Specialist]:
        """Look a specialist up by id, or (case-insensitively) by name."""
        ref = (ref or "").strip()
        if not ref:
            return None
        for s in self.specialists:
            if s.id == ref:
                return s
        for s in self.specialists:
            if s.name.lower() == ref.lower():
                return s
        return None

    # -- specialist transcripts -------------------------------------------
    # Kept out of state.json (they are large) in their own files, so a specialist
    # can be RESUMED with everything it already learned instead of being cloned.
    @property
    def transcripts_dir(self) -> Path:
        return self.science_dir / "specialists"

    def save_transcript(self, sid: str, messages: list[dict]) -> None:
        try:
            self.transcripts_dir.mkdir(parents=True, exist_ok=True)
            (self.transcripts_dir / f"{sid}.json").write_text(json.dumps(messages))
        except Exception:
            pass

    def load_transcript(self, sid: str) -> list[dict]:
        try:
            p = self.transcripts_dir / f"{sid}.json"
            return json.loads(p.read_text()) if p.exists() else []
        except Exception:
            return []

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
                    a.interim = art.interim
                    a.by = art.by or a.by
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
            "specialist_models": self.specialist_models,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            # Shallow copies: taken in one step, so another thread appending
            # meanwhile cannot change what is being written.
            "messages": list(self.messages),
            "specialists": [asdict(s) for s in list(self.specialists)],
            "jobs": [asdict(j) for j in list(self.jobs)],
            "artifacts": [asdict(a) for a in list(self.artifacts)],
            "provenance": list(self.provenance),
            "remote_id": self.remote_id,
        }

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.science_dir.mkdir(parents=True, exist_ok=True)
        # Specialists run on their own threads and save their records while the
        # orchestrator is still extending its message history (which it does
        # without this lock). The encoder can then meet a dict or list mid-change
        # and raise; that is transient, so take a fresh snapshot and retry. If it
        # keeps happening, skip this write — everything is still in memory and
        # the next save catches up.
        for _ in range(5):
            try:
                text = json.dumps(self.to_dict(), indent=2)
                break
            except RuntimeError:
                time.sleep(0.02)
        else:
            return
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(text)
        os.replace(tmp, self.state_path)

    def _write_provenance_md_locked(self) -> None:
        """Rewrite PROVENANCE.md.

        The point of this file is that someone opening it cold can tell what was
        produced and how to regenerate it. So it leads with a table of every
        output and whether its method is on record, names each entry by its file
        rather than by a raw path, and says plainly which fields each entry holds.
        Outputs with nothing recorded are listed too — a silent gap is the one
        thing a provenance record must not have.
        """
        by_art: dict[str, dict] = {}
        for e in self.provenance:
            by_art[str(e.get("artifact", "")).strip()] = e

        def _name(path: str) -> str:
            path = (path or "").strip()
            if not path:
                return "(unnamed output)"
            first = path.split(",")[0].split(" ")[0].rstrip("/")
            base = first.rsplit("/", 1)[-1]
            return base or first

        lines = [
            "# Provenance",
            "",
            f"How every output in this session was produced — what it was made "
            f"from, by which method, and when — so that it can be regenerated or "
            f"checked without asking anyone.",
            "",
            f"- **Session:** {self.task.splitlines()[0][:120] if self.task else '(untitled)'}",
            f"- **Working directory:** `{self.working_dir}`",
            f"- **Started:** {self.created_at}  ·  **Last updated:** {self.updated_at}",
            "",
            "Each entry below records:",
            "",
            "| field | meaning |",
            "| --- | --- |",
            "| **output** | the file this entry is about, and where it lives |",
            "| **made** | when it was produced |",
            "| **from** | the inputs it was derived from (data, structures, "
            "accessions, earlier outputs) |",
            "| **how** | the method: the steps, tools and parameters used |",
            "| **notes** | what the result shows, and any caveat or limitation |",
            "",
        ]

        # Contents: every output the session surfaced, plus any entry recorded
        # for something that was never presented.
        rows: list[tuple[str, str, str, bool]] = []
        seen: set[str] = set()
        for a in self.artifacts:
            key = a.rel or a.path
            seen.add(key)
            rec = by_art.get(key) or by_art.get(a.path) or by_art.get(a.rel)
            rows.append((_name(key), key, a.kind, rec is not None))
        for key in by_art:
            if key and key not in seen and key not in {a.path for a in self.artifacts}:
                rows.append((_name(key), key, "—", True))
        if rows:
            lines += ["## Outputs", "",
                      "| output | kind | method recorded |", "| --- | --- | --- |"]
            for name, key, kind, has in rows:
                lines.append(f"| `{key}` | {kind} | {'yes' if has else '**not yet**'} |")
            missing = [k for _, k, _, has in rows if not has]
            lines.append("")
            if missing:
                lines.append(
                    f"> {len(missing)} output(s) above have no method recorded yet, "
                    f"so they cannot be reproduced from this file alone.")
                lines.append("")

        if not self.provenance:
            lines += ["## Entries", "", "_Nothing recorded yet._", ""]
            self.provenance_path.write_text("\n".join(lines))
            return

        lines += ["## Entries", ""]
        for i, e in enumerate(self.provenance, 1):
            art = str(e.get("artifact", "")).strip()
            lines.append(f"### {i}. {_name(art)}")
            lines.append("")
            lines.append(f"- **output:** `{art or '(unnamed)'}`")
            lines.append(f"- **made:** {e.get('at', '(unknown)')}")
            lines.append(f"- **from:** {e.get('inputs') or '_not recorded_'}")
            lines.append(f"- **how:** {e.get('method') or '_not recorded_'}")
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
        s.specialist_models = [m for m in (data.get("specialist_models") or [])
                               if isinstance(m, str) and m.strip()]
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
            "model": self.model,
            "specialist_models": self.specialist_models,
            "specialists": [asdict(s) for s in self.specialists],
            "jobs": [asdict(j) for j in self.jobs],
            "artifacts": [asdict(a) for a in self.artifacts],
            "provenance": self.provenance,
            "remote_id": self.remote_id,
        }


def _known(cls, d: dict) -> dict:
    fields = set(getattr(cls, "__dataclass_fields__", {}))
    return {k: v for k, v in d.items() if k in fields}
