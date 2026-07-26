"""
Lab shared state — the blackboard every component reads and writes.

The state is the single source of truth for a lab session: the human task, the
working directory, the current phase, the dynamically-assembled team, the live
agenda, the pending human injections, and an index of produced artifacts.

It is persisted two ways under ``<working_dir>/lab/``:
  - ``state.json``  — machine-readable, the canonical state (atomic write)
  - ``team.md`` / ``plan.md`` — human-readable mirrors for the file explorer

All mutation goes through ``LabSession`` methods which bump ``updated_at`` and
re-persist, so the web UI (which watches the files / receives events) always
sees a consistent snapshot.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

class LabPhase:
    """Canonical phase names for the runner loop (also surfaced in the UI)."""
    INIT = "init"
    ORIENT = "orientation"
    ASSEMBLE = "assemble_team"
    PLANNING = "planning"
    CRITIC_GATE = "critic_gate"
    IMPLEMENTATION = "implementation"
    INTEGRATION = "integration"
    REVIEW = "review"
    REPORT = "report"
    COMPLETE = "complete"
    STOPPED = "stopped"

    ORDER = [INIT, ORIENT, ASSEMBLE, PLANNING, CRITIC_GATE, IMPLEMENTATION,
             INTEGRATION, REVIEW, REPORT, COMPLETE]


# ---------------------------------------------------------------------------
# Agent specification — one dynamically-created specialist
# ---------------------------------------------------------------------------

# Statuses an agent can be in (surfaced as the roster card state).
AGENT_IDLE = "idle"
AGENT_WORKING = "working"
AGENT_DONE = "done"
AGENT_DISCARDED = "discarded"


@dataclass
class AgentSpec:
    """A single specialist the Director created for this problem.

    ``tools`` is an allowlist of tool *names* (must exist in the live tool
    registry); agent_runtime restricts the offered tool surface to these.
    """
    name: str                       # short display name, e.g. "Computational Biologist"
    role: str                       # one-line role title
    expertise: str                  # what they're expert in (free text)
    goal: str                       # what they're here to accomplish
    tools: list[str] = field(default_factory=list)
    model: str = ""                 # filled from config default (kimi-k2.7) if blank
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = AGENT_IDLE
    icon: str = "🧪"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSpec":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Lab session — the blackboard
# ---------------------------------------------------------------------------

@dataclass
class LabSession:
    task: str
    working_dir: str
    model: str = ""                          # default model for all roles (kimi-k2.7)
    autonomous: bool = True                  # True = run without pausing at gates
    phase: str = LabPhase.INIT
    status: str = "idle"                     # idle | running | paused | complete | stopped
    round: int = 0
    agenda: str = ""                         # current focus / next agenda
    team: list[AgentSpec] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)   # {path, kind, by, ts}
    meetings: list[dict] = field(default_factory=list)    # {n, type, agenda, path, ts}
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # --- non-persisted runtime fields ---
    _injections: list[str] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _emit = None  # optional event callback (set by the runner)

    # ---- paths -----------------------------------------------------------
    @property
    def lab_dir(self) -> Path:
        return Path(self.working_dir) / "lab"

    @property
    def state_path(self) -> Path:
        return self.lab_dir / "state.json"

    @property
    def projects_dir(self) -> Path:
        return self.lab_dir / "projects"

    @property
    def meetings_dir(self) -> Path:
        return self.lab_dir / "meetings"

    @property
    def tools_dir(self) -> Path:
        return self.lab_dir / "tools"

    def ensure_dirs(self) -> None:
        for p in (self.lab_dir, self.projects_dir, self.meetings_dir, self.tools_dir):
            p.mkdir(parents=True, exist_ok=True)

    # ---- team helpers ----------------------------------------------------
    @property
    def active_team(self) -> list[AgentSpec]:
        return [a for a in self.team if a.status != AGENT_DISCARDED]

    def get_agent(self, agent_id: str) -> AgentSpec | None:
        return next((a for a in self.team if a.id == agent_id), None)

    def set_team(self, specs: list[AgentSpec]) -> None:
        # Default the model on any spec that didn't get one.
        for s in specs:
            if not s.model:
                s.model = self.model
        self.team = specs[:10]   # hard cap at 10 (vision constraint)
        self.touch()

    def set_agent_status(self, agent_id: str, status: str) -> None:
        a = self.get_agent(agent_id)
        if a:
            a.status = status
            self.touch()

    # ---- human injection (thread-safe) -----------------------------------
    def add_injection(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._injections.append(text)

    def drain_injections(self) -> list[str]:
        with self._lock:
            out = self._injections[:]
            self._injections.clear()
        return out

    def has_injections(self) -> bool:
        with self._lock:
            return bool(self._injections)

    # ---- artifacts / meetings -------------------------------------------
    def add_artifact(self, path: str, kind: str = "file", by: str = "") -> None:
        self.artifacts.append({"path": path, "kind": kind, "by": by, "ts": time.time()})
        self.touch()

    def add_meeting(self, n: int, mtype: str, agenda: str, path: str) -> None:
        self.meetings.append({"n": n, "type": mtype, "agenda": agenda,
                              "path": path, "ts": time.time()})
        self.touch()

    # ---- persistence -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "working_dir": self.working_dir,
            "model": self.model,
            "autonomous": self.autonomous,
            "phase": self.phase,
            "status": self.status,
            "round": self.round,
            "agenda": self.agenda,
            "team": [a.to_dict() for a in self.team],
            "artifacts": self.artifacts,
            "meetings": self.meetings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def touch(self) -> None:
        """Bump the timestamp and re-persist all mirrors."""
        self.updated_at = time.time()
        try:
            self.save()
        except Exception:
            pass

    def save(self) -> None:
        self.ensure_dirs()
        # Atomic write of state.json
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)
        # Human-readable mirrors
        self._write_team_md()
        self._write_plan_md()

    def _write_team_md(self) -> None:
        lines = [f"# Lab Team\n", f"**Task:** {self.task}\n",
                 f"**Phase:** {self.phase} · **Round:** {self.round}\n"]
        for a in self.active_team:
            lines.append(
                f"\n## {a.icon} {a.name}  `({a.status})`\n"
                f"- **Role:** {a.role}\n"
                f"- **Expertise:** {a.expertise}\n"
                f"- **Goal:** {a.goal}\n"
                f"- **Tools:** {', '.join(a.tools) or '—'}\n"
                f"- **Model:** {a.model}\n"
            )
        try:
            (self.lab_dir / "team.md").write_text("".join(lines), encoding="utf-8")
        except Exception:
            pass

    def _write_plan_md(self) -> None:
        lines = [f"# Lab Plan\n\n**Task:** {self.task}\n\n",
                 f"**Current phase:** {self.phase}  ·  **Round:** {self.round}\n\n",
                 f"## Current agenda\n\n{self.agenda or '_(not set yet)_'}\n\n",
                 "## Meetings\n\n"]
        for m in self.meetings:
            lines.append(f"- #{m['n']:03d} [{m['type']}] {m['agenda']}  →  `{m['path']}`\n")
        try:
            (self.lab_dir / "plan.md").write_text("".join(lines), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def load(cls, working_dir: str) -> "LabSession | None":
        sp = Path(working_dir) / "lab" / "state.json"
        if not sp.exists():
            return None
        try:
            d = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return None
        sess = cls(task=d.get("task", ""), working_dir=working_dir)
        sess.model = d.get("model", "")
        sess.autonomous = d.get("autonomous", True)
        sess.phase = d.get("phase", LabPhase.INIT)
        sess.status = d.get("status", "idle")
        sess.round = d.get("round", 0)
        sess.agenda = d.get("agenda", "")
        sess.team = [AgentSpec.from_dict(a) for a in d.get("team", [])]
        sess.artifacts = d.get("artifacts", [])
        sess.meetings = d.get("meetings", [])
        sess.created_at = d.get("created_at", time.time())
        sess.updated_at = d.get("updated_at", time.time())
        return sess
