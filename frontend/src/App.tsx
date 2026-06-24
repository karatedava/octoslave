import { useEffect, useMemo, useRef, useState } from "react";
import { LabSocket } from "./ws";
import type { Agent, ActivityItem, Decision } from "./types";

const PHASES = [
  ["orientation", "Orient"],
  ["assemble_team", "Assemble"],
  ["planning", "Plan"],
  ["implementation", "Implement"],
  ["review", "Review"],
  ["report", "Report"],
  ["complete", "Done"],
];

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
const lsNum = (k: string, d: number) => {
  const v = parseFloat(localStorage.getItem(k) || "");
  return Number.isFinite(v) ? v : d;
};

/** Split a free-text agenda into discrete to-do items so the user can read the
 * plan at a glance instead of watching raw project files. Handles "1) / 1. / -
 * / • / ;" structure; falls back to one prose item. */
function parseTodos(agenda: string): { items: string[]; guidance: string[] } {
  if (!agenda.trim()) return { items: [], guidance: [] };
  let main = agenda;
  const guidance: string[] = [];
  const gi = agenda.search(/human guidance:/i);
  if (gi >= 0) {
    main = agenda.slice(0, gi);
    agenda
      .slice(gi)
      .replace(/human guidance:/i, "")
      .split(/\n|(?:^|\s)-\s|•/)
      .map((s) => s.trim())
      .filter((s) => s.length > 2)
      .forEach((s) => guidance.push(s));
  }
  let parts = main
    .split(/(?=(?:^|\s)\d+[\).]\s)|\n\s*[-•*]\s|;\s+|\n{2,}/)
    .map((s) => s.replace(/^[\s\-•*]+/, "").replace(/^\d+[\).]\s*/, "").trim())
    .filter((s) => s.length > 2);
  if (parts.length <= 1) {
    // No clear structure — split on sentences as a softer fallback.
    parts = main
      .split(/(?<=[.!?])\s+(?=[A-Z])/)
      .map((s) => s.trim())
      .filter((s) => s.length > 2);
  }
  return { items: parts, guidance };
}

export default function App() {
  const sock = useMemo(() => new LabSocket(), []);
  const [connected, setConnected] = useState(false);
  const [started, setStarted] = useState(false);
  const [phase, setPhase] = useState<string>("init");
  const [round, setRound] = useState<number>(0);
  const [status, setStatus] = useState<string>("idle");
  const [autonomous, setAutonomous] = useState(true);
  const [awaiting, setAwaiting] = useState<null | { kind: string }>(null);

  const [task, setTask] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [rounds, setRounds] = useState(3);

  const [team, setTeam] = useState<Agent[]>([]);
  const [agenda, setAgenda] = useState("");
  const [objective, setObjective] = useState("");
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [agentLast, setAgentLast] = useState<Record<string, string>>({});
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [reportPath, setReportPath] = useState("");
  const [inject, setInject] = useState("");
  const [attachments, setAttachments] = useState<{ name: string; path: string }[]>([]);
  const attachInput = useRef<HTMLInputElement>(null);
  const actId = useRef(0);

  // --- resizable layout (persisted) ---
  const [leftW, setLeftW] = useState(() => lsNum("lab.leftW", 340));
  const [rightW, setRightW] = useState(() => lsNum("lab.rightW", 360));
  const [leftSplit, setLeftSplit] = useState(() => lsNum("lab.leftSplit", 50)); // % height of Team
  const leftColRef = useRef<HTMLDivElement>(null);
  useEffect(() => { localStorage.setItem("lab.leftW", String(leftW)); }, [leftW]);
  useEffect(() => { localStorage.setItem("lab.rightW", String(rightW)); }, [rightW]);
  useEffect(() => { localStorage.setItem("lab.leftSplit", String(leftSplit)); }, [leftSplit]);

  function dragCol(which: "left" | "right", e: React.MouseEvent) {
    e.preventDefault();
    const x0 = e.clientX, l0 = leftW, r0 = rightW;
    const move = (ev: MouseEvent) => {
      if (which === "left") setLeftW(clamp(l0 + (ev.clientX - x0), 240, 620));
      else setRightW(clamp(r0 - (ev.clientX - x0), 260, 680));
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    document.body.style.cursor = "col-resize";
  }

  function dragLeftSplit(e: React.MouseEvent) {
    e.preventDefault();
    const move = (ev: MouseEvent) => {
      const el = leftColRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      setLeftSplit(clamp(((ev.clientY - r.top) / r.height) * 100, 18, 82));
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    document.body.style.cursor = "row-resize";
  }

  function push(kind: string, text: string, tone?: ActivityItem["tone"], agent?: string) {
    setActivity((a) => [
      ...a.slice(-400),
      { id: actId.current++, kind, text, tone, agent, ts: Date.now() },
    ]);
  }

  function setAgentStatus(id: string, st: string) {
    setTeam((t) => t.map((a) => (a.id === id ? { ...a, status: st } : a)));
  }

  useEffect(() => {
    sock.onOpen(() => setConnected(true));
    sock.onMessage((m) => {
      switch (m.type) {
        case "lab_start":
          setStarted(true);
          setStatus("running");
          setWorkingDir(m.working_dir || workingDir);
          setAutonomous(!!m.autonomous);
          push("system", `Lab started — ${m.task}`, "info");
          break;
        case "lab_phase":
          setPhase(m.phase);
          if (m.round) setRound(m.round);
          push("phase", `Phase: ${m.phase}${m.round ? ` (round ${m.round})` : ""}`, "info");
          break;
        case "orientation":
          if (m.brief) setObjective(m.brief);
          push("system", `🧭 Orientation brief ready`, "info");
          break;
        case "team_update":
          setTeam(m.team || []);
          push("system", `Team set: ${(m.team || []).map((a: Agent) => a.name).join(", ")}`, "info");
          break;
        case "plan_update":
          setAgenda(m.agenda || "");
          break;
        case "agent_event":
          if (m.event === "start") {
            setAgentStatus(m.agent_id, "working");
            setAgentLast((p) => ({ ...p, [m.agent_id]: "starting…" }));
            push("agent", `${m.agent} started`, "info", m.agent);
          } else if (m.event === "done") {
            setAgentStatus(m.agent_id, "done");
            setAgentLast((p) => ({ ...p, [m.agent_id]: `done (${m.iterations} steps)` }));
            push("agent", `${m.agent} finished (${m.iterations} steps)`, "ok", m.agent);
          } else if (m.event === "tool_call") {
            setAgentLast((p) => ({ ...p, [m.agent_id]: `→ ${m.tool}` }));
            push("tool", `${m.agent} → ${m.tool}`, "info", m.agent);
          } else if (m.event === "tool_result") {
            push("tool", `${m.tool} ${m.ok ? "ok" : "failed"}`, m.ok ? "ok" : "bad", m.agent);
          } else if (m.event === "heartbeat") {
            // Liveness during a long turn — update the agent's live status only;
            // do NOT push an activity row (would spam the log).
            setAgentStatus(m.agent_id, "working");
            setAgentLast((p) => ({ ...p, [m.agent_id]: `working… ${m.elapsed ?? 0}s (turn ${m.iteration ?? "?"})` }));
          }
          break;
        case "meeting_start":
          push("meeting", `Meeting #${m.n} [${m.meeting_type}]: ${m.agenda || m.agent || ""}`, "info");
          break;
        case "meeting_turn":
          push("meeting", `${m.agent}: ${m.text}`, undefined, m.agent);
          break;
        case "meeting_end":
          if (m.synthesis) push("meeting", `Synthesis: ${m.synthesis}`, "info");
          break;
        case "critic_verdict":
          push(
            "critic",
            `Critic: ${String(m.verdict).toUpperCase()} — ${m.summary}`,
            m.verdict === "approve" ? "ok" : m.verdict === "reject" ? "bad" : "warn"
          );
          break;
        case "director_decision":
          setDecisions((d) => [
            ...d.slice(-40),
            { action: m.action, reason: m.reason, next_agenda: m.next_agenda || m.agenda || "",
              round, ts: Date.now() },
          ]);
          push("director", `Director: ${m.action} — ${m.reason}`, "info");
          break;
        case "tool_built":
          push("foundry", `🛠 Built tool "${m.name}" — ${m.purpose || ""}`, "ok");
          break;
        case "mcp_connected":
          push("foundry", `🔌 Connected MCP server "${m.server_id}"`, "ok");
          break;
        case "injection_applied":
          push("injection", `Human guidance applied:\n${m.text}`, "warn");
          break;
        case "report_ready":
          if (m.path) setReportPath(m.path);
          break;
        case "lab_awaiting":
          setAwaiting({ kind: m.kind });
          setStatus("paused");
          push("system", `Awaiting approval (${m.kind})`, "warn");
          break;
        case "lab_complete":
          setPhase(m.stopped ? "stopped" : "complete");
          setStatus(m.stopped ? "stopped" : "complete");
          if (m.report) setReportPath(m.report);
          push(
            "system",
            (m.stopped ? "Lab stopped." : "Lab complete.") +
              " You can refine below — small notes patch the report, bigger ones run more rounds.",
            m.stopped ? "bad" : "ok"
          );
          break;
        case "info":
          push("system", m.text, "info");
          break;
        case "error":
          push("system", m.text, "bad");
          break;
        default:
          break;
      }
    });
    sock.connect();
  }, []);

  function start() {
    if (!task.trim() || !workingDir.trim()) return;
    setActivity([]);
    setTeam([]);
    setReportPath("");
    setRound(0);
    setObjective("");
    setDecisions([]);
    setAgentLast({});
    sock.send({ type: "start_lab", task, working_dir: workingDir, rounds, autonomous });
  }

  async function browseDir() {
    try {
      const r = await fetch("/api/pick-dir");
      const d = await r.json();
      if (d.path) setWorkingDir(d.path);
    } catch {
      /* native dialog unavailable — user can still type the path */
    }
  }

  async function onAttach(files: FileList | null) {
    if (!files || !workingDir) return;
    for (const f of Array.from(files)) {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("working_dir", workingDir);
      try {
        const r = await fetch("/api/upload", { method: "POST", body: fd });
        const d = await r.json();
        if (d.path) setAttachments((a) => [...a, { name: d.name, path: d.path }]);
      } catch {
        /* ignore upload failure */
      }
    }
    if (attachInput.current) attachInput.current.value = "";
  }

  function sendInject() {
    if (!inject.trim() && attachments.length === 0) return;
    let text = inject.trim();
    if (attachments.length) {
      text +=
        (text ? "\n\n" : "") +
        "Attached files (saved in the working directory — read them):\n" +
        attachments.map((a) => `- ${a.path}`).join("\n");
    }
    const tag = attachments.length ? ` [+${attachments.length} file(s)]` : "";
    if (status === "running") {
      sock.send({ type: "inject", text });
      push("injection", `You: ${inject}${tag}`, "warn");
    } else {
      sock.send({ type: "lab_followup", text, working_dir: workingDir });
      push("injection", `Follow-up: ${inject}${tag}`, "warn");
      setStatus("running");
    }
    setInject("");
    setAttachments([]);
  }

  function toggleAutonomous() {
    const v = !autonomous;
    setAutonomous(v);
    sock.send({ type: "set_autonomous", autonomous: v });
  }

  function approve() {
    sock.send({ type: "approve" });
    setAwaiting(null);
    setStatus("running");
  }
  function stop() {
    sock.send({ type: "stop_lab" });
    setAwaiting(null);
  }

  const running = started && status === "running";
  const followupMode = status === "complete" || status === "stopped";
  const canType = status === "running" || followupMode;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">🧬 OctoSlave <span>Lab</span></div>
        <div className="phasebar">
          {PHASES.map(([key, label]) => (
            <div
              key={key}
              className={
                "phase-pill" +
                (phase === key ? " active" : "") +
                (phaseIndex(phase) > phaseIndex(key) ? " past" : "")
              }
            >
              {label}
            </div>
          ))}
        </div>
        <div className="topright">
          {started && round > 0 && (
            <span className="round-badge" title="Current implementation/review round">
              Round {round}
            </span>
          )}
          {started && (
            <label className="toggle">
              <input type="checkbox" checked={autonomous} onChange={toggleAutonomous} />
              Autonomous
            </label>
          )}
          <span className={"dot " + (connected ? "on" : "off")} title={connected ? "Connected" : "Disconnected"} />
          {running && <button className="btn danger" onClick={stop}>Stop</button>}
        </div>
      </header>

      {!started ? (
        <StartForm
          task={task} setTask={setTask}
          workingDir={workingDir} setWorkingDir={setWorkingDir}
          rounds={rounds} setRounds={setRounds}
          autonomous={autonomous} setAutonomous={setAutonomous}
          onStart={start}
          onBrowse={browseDir}
          connected={connected}
        />
      ) : (
        <div
          className="grid"
          style={{ gridTemplateColumns: `${leftW}px 7px minmax(0,1fr) 7px ${rightW}px` }}
        >
          <aside className="col left" ref={leftColRef}>
            <div className="vstack" style={{ flexBasis: `${leftSplit}%` }}>
              <Section title={`Team (${team.length})`}>
                <Roster team={team} last={agentLast} />
              </Section>
            </div>
            <div className="vhandle" onMouseDown={dragLeftSplit} title="Drag to resize" />
            <div className="vstack" style={{ flex: 1 }}>
              <Section title="Plan & To-Do">
                <PlanPanel agenda={agenda} objective={objective} decisions={decisions} round={round} />
              </Section>
            </div>
          </aside>

          <div className="handle" onMouseDown={(e) => dragCol("left", e)} title="Drag to resize" />

          <main className="col center">
            <Section title="Activity">
              <Activity items={activity} />
            </Section>
          </main>

          <div className="handle" onMouseDown={(e) => dragCol("right", e)} title="Drag to resize" />

          <aside className="col right">
            <Section title="Files">
              <FileExplorer workingDir={workingDir} reportPath={reportPath} />
            </Section>
          </aside>
        </div>
      )}

      {started && (
        <footer className="injectbar">
          {awaiting ? (
            <div className="awaiting">
              <span>Paused for approval ({awaiting.kind}).</span>
              <button className="btn primary" onClick={approve}>Approve & continue</button>
              <button className="btn danger" onClick={stop}>Stop</button>
            </div>
          ) : (
            <>
              {attachments.length > 0 && (
                <div className="inject-attachments">
                  {attachments.map((a, i) => (
                    <span key={i} className="attach-chip" title={a.path}>
                      📎 {a.name}
                      <button
                        className="attach-x"
                        onClick={() => setAttachments((arr) => arr.filter((_, j) => j !== i))}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <input
                ref={attachInput}
                type="file"
                multiple
                style={{ display: "none" }}
                onChange={(e) => onAttach(e.target.files)}
              />
              <button
                className="btn icon"
                title="Attach files (saved into the working directory)"
                onClick={() => attachInput.current?.click()}
                disabled={!canType}
              >
                📎
              </button>
              <input
                className="inject-input"
                placeholder={
                  followupMode
                    ? "Refine: e.g. 'add a methods table' (report fix) or 'also test XGBoost' (more rounds)…"
                    : "Inject insight or guidance for the team…"
                }
                value={inject}
                onChange={(e) => setInject(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && sendInject()}
                disabled={!canType}
              />
              <button className="btn primary" onClick={sendInject} disabled={!canType}>
                {followupMode ? "Refine" : "Inject"}
              </button>
            </>
          )}
        </footer>
      )}
    </div>
  );
}

function phaseIndex(p: string): number {
  const order = ["init", "orientation", "assemble_team", "planning", "critic_gate",
    "implementation", "integration", "review", "report", "complete", "stopped"];
  const i = order.indexOf(p);
  return i < 0 ? -1 : i;
}

function Section({ title, children, right }: { title: string; children: any; right?: any }) {
  return (
    <section className="panel">
      <div className="panel-title">
        <span>{title}</span>
        {right}
      </div>
      <div className="panel-body">{children}</div>
    </section>
  );
}

function PlanPanel({
  agenda, objective, decisions, round,
}: { agenda: string; objective: string; decisions: Decision[]; round: number }) {
  const [showObj, setShowObj] = useState(false);
  const { items, guidance } = parseTodos(agenda);
  const recentDecisions = decisions.slice(-6).reverse();
  return (
    <div className="plan">
      {objective && (
        <div className="plan-block">
          <button className="plan-h collapsible" onClick={() => setShowObj((v) => !v)}>
            <span>🎯 Objective</span>
            <span className="chev">{showObj ? "▾" : "▸"}</span>
          </button>
          {showObj && <div className="objective">{objective}</div>}
        </div>
      )}

      <div className="plan-block">
        <div className="plan-h">
          <span>✅ Current to-do{round ? ` · round ${round}` : ""}</span>
        </div>
        {items.length ? (
          <ul className="todo">
            {items.map((t, i) => (
              <li key={i} className="todo-item">
                <span className="todo-dot" />
                <span>{t}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">Waiting for the Director to set the plan…</div>
        )}
        {guidance.length > 0 && (
          <div className="guidance">
            <div className="guidance-h">💉 Your guidance folded in</div>
            <ul className="todo">
              {guidance.map((t, i) => (
                <li key={i} className="todo-item warn">
                  <span className="todo-dot" />
                  <span>{t}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {recentDecisions.length > 0 && (
        <div className="plan-block">
          <div className="plan-h"><span>🧠 Director decisions</span></div>
          <div className="decisions">
            {recentDecisions.map((d, i) => (
              <div key={i} className={"decision " + d.action}>
                <div className="decision-head">
                  <span className={"decision-action " + d.action}>{d.action}</span>
                  {d.round ? <span className="decision-round">round {d.round}</span> : null}
                </div>
                {d.reason && <div className="decision-reason">{d.reason}</div>}
                {d.next_agenda && d.action !== "report" && (
                  <div className="decision-next">→ {d.next_agenda}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Roster({ team, last }: { team: Agent[]; last: Record<string, string> }) {
  if (!team.length) return <div className="muted">Assembling…</div>;
  return (
    <div className="roster">
      {team.map((a) => (
        <div key={a.id} className={"agent-card " + a.status}>
          <div className="agent-head">
            <span className="agent-icon">{a.icon}</span>
            <span className="agent-name">{a.name}</span>
            <span className={"agent-status " + a.status}>{a.status}</span>
          </div>
          <div className="agent-role">{a.role}</div>
          {a.goal && <div className="agent-goal">{a.goal}</div>}
          {last[a.id] && a.status === "working" && (
            <div className="agent-live">▶ {last[a.id]}</div>
          )}
          <div className="agent-tools">
            {a.tools.length ? a.tools.map((t) => <span key={t} className="tool-chip">{t}</span>)
              : <span className="muted">advisor (no tools)</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

function Activity({ items }: { items: ActivityItem[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [filter, setFilter] = useState<string>("all");
  const kinds = ["all", "director", "critic", "meeting", "injection", "tool", "agent"];
  const shown = filter === "all" ? items : items.filter((it) => it.kind === filter);
  useEffect(() => {
    ref.current?.scrollTo(0, ref.current.scrollHeight);
  }, [shown.length]);
  return (
    <div className="activity-wrap">
      <div className="act-filter">
        {kinds.map((k) => (
          <button
            key={k}
            className={"act-fbtn" + (filter === k ? " on" : "")}
            onClick={() => setFilter(k)}
          >
            {k}
          </button>
        ))}
      </div>
      <div className="activity" ref={ref}>
        {shown.map((it) => (
          <div key={it.id} className={"act-row " + it.kind + (it.tone ? " " + it.tone : "")}>
            <span className={"act-tag " + it.kind}>{it.kind}</span>
            <span className="act-text">{it.text}</span>
          </div>
        ))}
        {!shown.length && <div className="muted">No {filter} events yet.</div>}
      </div>
    </div>
  );
}

function FileExplorer({ workingDir, reportPath }: { workingDir: string; reportPath: string }) {
  const [files, setFiles] = useState<string[]>([]);
  const [sel, setSel] = useState("");

  async function refresh() {
    if (!workingDir) return;
    try {
      const r = await fetch(`/api/picker?working_dir=${encodeURIComponent(workingDir)}&q=lab/`);
      const d = await r.json();
      setFiles(d.items || []);
    } catch {
      /* ignore */
    }
  }
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [workingDir]);

  const viewUrl = (rel: string) =>
    `/api/files/view/${encodeURIComponent(workingDir.replace(/\/$/, "") + "/" + rel)}`;

  return (
    <div className="files">
      {reportPath && (
        <a className="report-link" href={viewUrl(reportPath)} target="_blank" rel="noreferrer">
          📊 Open final report
        </a>
      )}
      <div className="file-list">
        {files.length === 0 && <div className="muted">No files yet.</div>}
        {files.map((f) => (
          <div
            key={f}
            className={"file-row" + (sel === f ? " sel" : "")}
            onClick={() => setSel(f)}
            title={f}
          >
            {f}
          </div>
        ))}
      </div>
      {sel && (
        <div className="file-view">
          <div className="file-view-head">
            <span>{sel}</span>
            <a href={viewUrl(sel)} target="_blank" rel="noreferrer">open ↗</a>
          </div>
          <iframe className="file-frame" src={viewUrl(sel)} title="file" />
        </div>
      )}
    </div>
  );
}

function StartForm(props: {
  task: string; setTask: (s: string) => void;
  workingDir: string; setWorkingDir: (s: string) => void;
  rounds: number; setRounds: (n: number) => void;
  autonomous: boolean; setAutonomous: (b: boolean) => void;
  onStart: () => void; onBrowse: () => void; connected: boolean;
}) {
  return (
    <div className="startwrap">
      <div className="startcard">
        <h1>Start a Lab</h1>
        <p className="muted">
          Describe a problem — scientific or not. A Director will assemble a team of
          specialists, a Critic will challenge the plan, and the team will work
          autonomously while you watch and inject guidance.
        </p>
        <label>Task</label>
        <textarea
          rows={5}
          placeholder="e.g. Analyze this CSV of sales data and produce a forecast report…"
          value={props.task}
          onChange={(e) => props.setTask(e.target.value)}
        />
        <label>Working directory</label>
        <div className="dir-row">
          <input
            placeholder="Choose a folder, or paste an absolute path…"
            value={props.workingDir}
            onChange={(e) => props.setWorkingDir(e.target.value)}
          />
          <button className="btn" type="button" onClick={props.onBrowse}>
            Browse…
          </button>
        </div>
        <div className="start-row">
          <div>
            <label>Max rounds</label>
            <input
              type="number" min={1} max={12} value={props.rounds}
              onChange={(e) => props.setRounds(parseInt(e.target.value || "3"))}
            />
          </div>
          <label className="toggle big">
            <input
              type="checkbox"
              checked={props.autonomous}
              onChange={(e) => props.setAutonomous(e.target.checked)}
            />
            Autonomous (don't pause at gates)
          </label>
        </div>
        <button
          className="btn primary big"
          onClick={props.onStart}
          disabled={!props.connected || !props.task.trim() || !props.workingDir.trim()}
        >
          {props.connected ? "Start Lab" : "Connecting…"}
        </button>
      </div>
    </div>
  );
}
