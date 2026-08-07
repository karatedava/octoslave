import { useEffect, useMemo, useRef, useState } from "react";
import { LabSocket } from "./ws";
import { Nav } from "./Nav";
import { ModelPicker } from "./ModelPicker";
import { AskCard, askFromEvent, type Ask } from "./AskCard";

type Role = "user" | "assistant" | "note";
type Artifact = {
  id: string;
  rel: string;
  path: string;
  caption: string;
  kind: string;
  provenance?: string;
  comments?: { text: string; at: string }[];
  ver?: number;   // bumped on each update → cache-busts the preview URL
};
// `who`/`icon` name the agent that produced the item — absent means the
// orchestrator itself; set while a spawned specialist holds the floor.
type Speaker = { who?: string; icon?: string };
type Feed =
  | ({ t: "msg"; id: number; role: Role; text: string; tone?: string } & Speaker)
  | { t: "art"; id: number; art: Artifact };

type Specialist = {
  id: string; name: string; role: string; goal: string;
  icon: string; status: string; summary: string; tools: string[];
};
type Job = {
  id: string; name: string; status: string; remote_label: string;
  scheduler: string; handle: string; output?: string;
};
type Prov = { artifact: string; method?: string; inputs?: string; notes?: string; at?: string };
type SessionMeta = {
  working_dir: string; title: string; created_at: string; updated_at: string; running?: boolean;
};

const EDITABLE = /\.(md|txt|csv|tsv|json|ya?ml|html?|py|sh|svg)$/i;

export default function Science() {
  const sock = useMemo(() => new LabSocket(), []);
  const [connected, setConnected] = useState(false);
  const [started, setStarted] = useState(false);
  const [running, setRunning] = useState(false);
  const [workingDir, setWorkingDir] = useState("");
  const [goal, setGoal] = useState("");

  const [feed, setFeed] = useState<Feed[]>([]);
  const [live, setLive] = useState("");            // streaming assistant draft
  const [liveWho, setLiveWho] = useState<Speaker>({});  // who is speaking right now
  const [input, setInput] = useState("");
  const [specialists, setSpecialists] = useState<Specialist[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [prov, setProv] = useState<Prov[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [side, setSide] = useState<"outputs" | "team" | "jobs" | "fair">("outputs");
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [showSessions, setShowSessions] = useState(false);
  const [remoteRunning, setRemoteRunning] = useState(false);
  const [remotes, setRemotes] = useState<{ id: string; name?: string; host?: string }[]>([]);
  const [remoteId, setRemoteId] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [orchModel, setOrchModel] = useState("");
  const [pool, setPool] = useState<string[]>([]);
  const [todos, setTodos] = useState<{ content: string; status: string }[]>([]);
  const [showPlan, setShowPlan] = useState(true);
  // A pending ask_user. The agent thread is BLOCKED on this — until it is
  // answered (or expires) nothing else happens, so it gets its own always-visible
  // prompt rather than a line in the scrolling feed.
  const [ask, setAsk] = useState<Ask | null>(null);

  const feedId = useRef(0);
  // Live mirror of the run settings. The socket handler is registered once (on
  // mount), so anything it calls sees FIRST-RENDER state — which is how the
  // opening message of a session used to go out with no model and land on the
  // backend default. Reads go through this ref, never the closure.
  const runCfg = useRef({ orchModel: "", pool: [] as string[], workingDir: "", remoteId: "" });
  const runningRef = useRef(false);   // same reason: the handler can't see `running`
  const lastAssistant = useRef("");
  const pendingFirst = useRef<string>("");
  const resuming = useRef(false);
  const artIds = useRef<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
  const reconnected = useRef(false);   // false only until the first socket open
  const pollTimer = useRef(0);         // set while polling a disconnected turn

  function push(item: Omit<Feed, "id">) {
    setFeed((f) => [...f, { ...(item as any), id: feedId.current++ }]);
  }
  function note(text: string, tone?: string, from?: Speaker) {
    push({ t: "msg", role: "note", text, tone, ...(from || {}) } as any);
  }
  function upsertArtifact(a: Artifact) {
    setArtifacts((arr) => {
      const i = arr.findIndex((x) => x.id === a.id);
      if (i >= 0) {
        const c = arr.slice();
        c[i] = { ...c[i], ...a, ver: (c[i].ver || 1) + 1 };
        return c;
      }
      return [...arr, { ...a, ver: 1 }];
    });
    setFeed((f) => {
      const i = f.findIndex((x) => x.t === "art" && x.art.id === a.id);
      if (i >= 0) {
        const copy = f.slice();
        const old = (copy[i] as any).art;
        copy[i] = { ...(copy[i] as any), art: { ...old, ...a, ver: (old.ver || 1) + 1 } };
        return copy;
      }
      return [...f, { t: "art", id: feedId.current++, art: { ...a, ver: 1 } }];
    });
  }

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [feed, live, ask]);

  useEffect(() => {
    runCfg.current = { orchModel, pool, workingDir, remoteId };
  }, [orchModel, pool, workingDir, remoteId]);

  useEffect(() => {
    fetch("/api/remotes")
      .then((r) => r.json())
      .then((d) => setRemotes(Array.isArray(d.remotes) ? d.remotes : []))
      .catch(() => {});
    fetch("/api/models")
      .then((r) => r.json())
      .then((d) => {
        const ms: string[] = Array.isArray(d.models) ? d.models : [];
        setModels(ms);
        // Only seed the default if nothing is chosen yet — this request races
        // with the session restore, and must never clobber a restored choice.
        setOrchModel((cur) =>
          cur || (d.default && ms.includes(d.default) ? d.default : ms[0] || ""));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    sock.onOpen(() => {
      setConnected(true);
      // A reconnect means a fresh server-side connection: a turn started on the
      // old socket still runs, but its events (including science_done) can never
      // reach us. Ask the server who is actually still working, so the tab can't
      // sit "running" forever — or claim to be idle while a turn is alive.
      if (reconnected.current) resyncRunning();
      reconnected.current = true;
    });
    sock.onClose(() => setConnected(false));
    sock.onMessage((m) => {
      switch (m.type) {
        case "science_user":
          push({ t: "msg", role: "user", text: m.text } as any);
          break;
        case "stream_start":
          setLiveWho(speakerOf(m));
          break;
        case "token":
          setLive((s) => s + (m.text || ""));
          setLiveWho(speakerOf(m));
          break;
        case "reasoning":
          break;
        case "stream_end": {
          const from = speakerOf(m);
          // An aborted stream is thrown away server-side — it never reaches the
          // message history, so the model will redo the turn from scratch.
          // Keeping the draft in the feed made a stalling run look like it was
          // repeatedly "about to" act, when in fact nothing had been said yet.
          if (m.aborted) {
            setLive((s) => {
              if (s.trim()) note("↺ That response was cut off mid-stream and discarded — "
                + "the model will redo this step.", "bad", from);
              return "";
            });
            setLiveWho({});
            break;
          }
          setLive((s) => {
            const t = s.trim();
            if (t) {
              lastAssistant.current = t;
              push({ t: "msg", role: "assistant", text: t, ...from } as any);
            }
            return "";
          });
          break;
        }
        case "science_reply": {
          const t = (m.text || "").trim();
          const from = speakerOf(m);
          setLive((s) => {
            const draft = s.trim();
            if (draft) {
              lastAssistant.current = draft;
              push({ t: "msg", role: "assistant", text: draft, ...from } as any);
            }
            return "";
          });
          setLiveWho({});
          if (t && t !== lastAssistant.current) {
            lastAssistant.current = t;
            push({ t: "msg", role: "assistant", text: t, tone: m.stopped ? "warn" : undefined } as any);
          }
          break;
        }
        // The ONLY end-of-turn signal: the server sends it once the turn thread
        // has actually finished. A reply alone isn't enough (the orchestrator can
        // keep working after one), and an error certainly isn't — a mid-run error
        // is often recovered from, e.g. a dead model swapped for a fallback.
        case "science_done":
          setLive((s) => {
            const draft = s.trim();
            if (draft && draft !== lastAssistant.current) {
              lastAssistant.current = draft;
              push({ t: "msg", role: "assistant", text: draft } as any);
            }
            return "";
          });
          setLiveWho({});
          runningRef.current = false;
          setRunning(false);
          // A turn can't end with a live question — if one is still on screen
          // (a stop, a crash), nothing is listening for the answer any more.
          setAsk(null);
          break;
        case "tool_call":
          note(`${toolIcon(m.name)} ${m.summary || m.name}`, "tool", speakerOf(m));
          break;
        case "tool_result":
          if (m.ok === false) note(`⚠ ${m.name} failed`, "bad", speakerOf(m));
          break;
        case "todos":
          setTodos((m.todos || []).map((t: any) =>
            ({ content: t.content || t.text || "", status: t.status || "pending" })));
          setShowPlan(true);
          break;
        case "plan":
          if (m.text) push({ t: "msg", role: "note", text: "🗺 Plan:\n" + m.text, tone: "info" } as any);
          break;
        case "science_specialist":
          setLiveWho({});
          if (m.event === "start") {
            // A resumed specialist keeps its slot (and its previous summary) —
            // it's the same agent picking its work back up, not a new one.
            setSpecialists((s) => {
              const prev = s.find((x) => x.id === m.id);
              const next = {
                id: m.id, name: m.name, role: m.role, goal: m.goal, icon: m.icon || "🔬",
                status: "working", summary: m.resumed ? prev?.summary || "" : "",
                tools: m.tools || [],
              };
              return prev ? s.map((x) => (x.id === m.id ? next : x)) : [...s, next];
            });
            note(m.resumed
              ? `${m.icon || "🔬"} Resumed specialist — ${m.name} (continuing its earlier work)`
              : `${m.icon || "🔬"} Spun up specialist — ${m.name} (${m.role})`, "spec");
            setSide("team");
          } else if (m.event === "done") {
            setSpecialists((s) => s.map((x) => x.id === m.id
              ? { ...x, status: m.status, summary: m.summary || "" } : x));
            note(`${m.status === "failed" ? "⚠" : "✓"} ${nameOf(m.id)} finished`,
              m.status === "failed" ? "bad" : "ok");
          }
          break;
        case "science_job":
          setJobs((j) => [...j.filter((x) => x.id !== m.id),
            { id: m.id, name: m.name, status: m.status, remote_label: m.remote,
              scheduler: m.scheduler, handle: m.handle, output: m.output }]);
          note(`🖥 Job "${m.name}" on ${m.remote}: ${m.status}`,
            m.status === "failed" ? "bad" : "job");
          break;
        case "science_artifact": {
          const isUpdate = artIds.current.has(m.id);
          artIds.current.add(m.id);
          upsertArtifact({ id: m.id, rel: m.rel, path: m.path, caption: m.caption,
            kind: m.kind, provenance: m.provenance });
          if (isUpdate) {
            note(`🔄 Updated — ${m.caption || m.rel}`, "ok", speakerOf(m));
            // scroll back to the refreshed card so the user sees the new version
            setTimeout(() => focusArtifact(m.id), 90);
          } else {
            note(`🖼 Output ready — ${m.caption || m.rel} (comment below to refine)`, "spec",
              speakerOf(m));
            setSide("outputs");
          }
          break;
        }
        case "science_provenance":
          setProv((p) => [...p, { ...(m.entry || {}) }]);
          break;
        case "science_state":
          // rehydrate an existing session
          if (m.exists) {
            (m.history || []).forEach((h: any) =>
              push({ t: "msg", role: h.role, text: h.text } as any));
            const snap = m.snapshot || {};
            // Reopening (or a refresh, which skips the start form) must keep the
            // models this session was started with — not fall back to the
            // backend's default_model. But when the user just picked models on
            // the start form (pendingFirst), THEIR choice wins over the stored one.
            if (!pendingFirst.current) {
              if (snap.model) {
                setOrchModel(snap.model);
                runCfg.current = { ...runCfg.current, orchModel: snap.model };
              }
              if (Array.isArray(snap.specialist_models) && snap.specialist_models.length) {
                setPool(snap.specialist_models);
                runCfg.current = { ...runCfg.current, pool: snap.specialist_models };
              }
            }
            setSpecialists(snap.specialists || []);
            setJobs(snap.jobs || []);
            setProv(snap.provenance || []);
            (snap.artifacts || []).forEach((a: Artifact) => {
              artIds.current.add(a.id);
              upsertArtifact(a);
            });
            // `attached` means the server hooked this socket up to a turn that is
            // still in flight and is replaying its backlog: this tab is a live
            // viewer, not a locked-out bystander. Only an unattachable run (no
            // hub — e.g. started before a server restart) stays "busy elsewhere".
            setRemoteRunning(!!m.running && !m.attached);
            if (m.attached) {
              runningRef.current = true;
              setRunning(true);
            } else if (m.running) {
              // Running but not attachable (no hub — the turn predates a server
              // restart). Nothing to stream, so fall back to polling for the end.
              pollUntilIdle(m.working_dir || workingDir);
            }
            if ((m.history || []).length) note("↩ Resumed a previous session.", "ok");
          } else if (resuming.current) {
            // auto-resume target no longer exists — fall back to the start screen
            setStarted(false);
            localStorage.removeItem("science.lastDir");
          }
          resuming.current = false;
          // fire the first message once the session state is known
          if (pendingFirst.current) {
            const first = pendingFirst.current;
            pendingFirst.current = "";
            sendMessage(first);
          }
          break;
        // The agent called ask_user and its thread is now blocked. Without a
        // prompt here the question is unanswerable: the composer is locked for
        // the duration of the turn, so the run just stalls until the server's
        // timeout fires and it proceeds without the answer.
        case "user_question":
          setAsk(askFromEvent(m));
          break;
        case "user_question_closed":
          setAsk((a) => {
            if (a && !m.answered) {
              note("⏳ That question timed out — the orchestrator is carrying on with "
                + "its own best judgement.", "warn");
            }
            return null;
          });
          break;
        case "info":
          note(m.text, "info");
          break;
        case "error":
          // Reported, but the turn keeps its running state until science_done.
          note(m.text, "bad");
          break;
      }
    });
    sock.connect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Events carry `agent_name`/`agent_icon` only while a spawned specialist holds
  // the floor; without them the orchestrator is speaking.
  function speakerOf(m: any): Speaker {
    return m && m.agent_name ? { who: m.agent_name, icon: m.agent_icon || "🔬" } : {};
  }

  function nameOf(id: string): string {
    return specialists.find((s) => s.id === id)?.name || "Specialist";
  }

  async function refreshSessions() {
    try {
      const r = await fetch("/api/science/sessions");
      const d = await r.json();
      setSessions(d.sessions || []);
    } catch { /* ignore */ }
  }

  async function isSessionAlive(dir: string): Promise<boolean> {
    const r = await fetch("/api/science/sessions");
    const d = await r.json();
    return !!(d.sessions || []).find((s: SessionMeta) => s.working_dir === dir)?.running;
  }

  // Called after the socket reconnects. The old connection's events are gone, so
  // reload the session on the NEW one: the server re-attaches us to the turn if
  // it is still in flight and replays what we missed. Reloading (rather than
  // patching state in place) also rebuilds the feed from scratch, so the restored
  // history can't be appended on top of what is already on screen.
  async function resyncRunning() {
    const dir = runCfg.current.workingDir || "";
    if (!dir) return;
    const wasRunning = runningRef.current;
    let alive = false;
    try {
      alive = await isSessionAlive(dir);
    } catch { /* treat as finished — better idle than stuck */ }
    if (!wasRunning && !alive) return;   // nothing was in flight; leave the view alone
    openSession(dir, "", true);          // clears the feed, so note AFTER it
    note(alive
      ? "⚠ Lost the live connection — reconnecting to the turn still running there…"
      : "⚠ Lost the live connection to the server — the turn is no longer running.", "bad");
  }

  function pollUntilIdle(dir: string) {
    if (pollTimer.current) return;
    const tick = async () => {
      pollTimer.current = 0;
      try {
        if (await isSessionAlive(dir)) {
          pollTimer.current = window.setTimeout(tick, 8000);
          return;
        }
      } catch {
        pollTimer.current = window.setTimeout(tick, 8000);
        return;
      }
      setRemoteRunning(false);
      note("✓ That turn finished — reopen the session to load what it produced.", "ok");
    };
    pollTimer.current = window.setTimeout(tick, 8000);
  }

  function resetView() {
    setFeed([]); setLive(""); setLiveWho({}); setSpecialists([]); setJobs([]); setProv([]);
    setTodos([]); setArtifacts([]);
    setRunning(false);
    setAsk(null);
    runningRef.current = false;
    if (pollTimer.current) { clearTimeout(pollTimer.current); pollTimer.current = 0; }
    lastAssistant.current = "";
    artIds.current = new Set();
  }

  function openSession(dir: string, firstMsg = "", isResume = false) {
    if (!dir.trim()) return;
    resetView();
    setWorkingDir(dir);
    // Synchronously, so the first message can't race the state update.
    runCfg.current = { ...runCfg.current, workingDir: dir };
    setStarted(true);
    setShowSessions(false);
    setRemoteRunning(false);
    localStorage.setItem("science.lastDir", dir);
    resuming.current = isResume;
    pendingFirst.current = firstMsg.trim();
    sock.send({ type: "science_load", working_dir: dir });
    refreshSessions();
  }

  function newSession() {
    setStarted(false);
    setShowSessions(false);
    resetView();
    setGoal("");
    localStorage.removeItem("science.lastDir");
  }

  async function forgetSession(dir: string, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await fetch(`/api/science/sessions?working_dir=${encodeURIComponent(dir)}`,
        { method: "DELETE" });
    } catch { /* ignore */ }
    refreshSessions();
  }

  // On load: fetch history and auto-resume the last session (survives refresh).
  useEffect(() => {
    refreshSessions();
    const last = localStorage.getItem("science.lastDir");
    if (last) openSession(last, "", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function browseDir() {
    try {
      const r = await fetch("/api/pick-dir");
      const d = await r.json();
      if (d.path) setWorkingDir(d.path);
    } catch { /* dialog unavailable */ }
  }

  function sendMessage(text: string) {
    if (!text.trim() || runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    const c = runCfg.current;
    sock.send({
      type: "science_message", message: text,
      working_dir: c.workingDir || workingDir,
      remote_id: c.remoteId || null,
      model: c.orchModel || undefined,
      specialist_models: c.pool,
    });
  }
  function onSend() {
    const t = input.trim();
    if (!t) return;
    setInput("");
    sendMessage(t);
  }
  function stop() {
    sock.send({ type: "stop" });
  }
  // Answer a blocked ask_user. This is NOT a new turn — the agent is parked
  // inside a tool call waiting on this exact reply, so it goes out as a
  // user_response and the running turn simply resumes.
  function answerQuestion(text: string) {
    const t = text.trim();
    if (!t || !ask) return;
    sock.send({ type: "user_response", answer: t });
    setAsk(null);
    push({ t: "msg", role: "user", text: t } as any);
  }
  function comment(artId: string, text: string) {
    if (!text.trim() || runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    // Same model/pool as a normal turn — a refine must not silently run on a
    // different model from the rest of the session.
    const c = runCfg.current;
    sock.send({ type: "science_comment", artifact_id: artId, text,
                working_dir: c.workingDir || workingDir,
                remote_id: c.remoteId || null, model: c.orchModel || undefined,
                specialist_models: c.pool });
  }
  function focusArtifact(id: string) {
    const el = document.getElementById("sciart-" + id);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("flash");
      setTimeout(() => el.classList.remove("flash"), 1400);
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">🧬 OctoSlave <span>Science</span></div>
        <Nav current="science" />
        <div className="topright">
          <div className="sci-sessions-wrap">
            <button className="btn" onClick={() => { setShowSessions((v) => !v); refreshSessions(); }}>
              🕘 Sessions
            </button>
            {showSessions && (
              <>
                <div className="sci-menu-scrim" onClick={() => setShowSessions(false)} />
                <SessionMenu
                  sessions={sessions}
                  current={started ? workingDir : ""}
                  onOpen={(d) => openSession(d)}
                  onNew={newSession}
                  onForget={forgetSession}
                />
              </>
            )}
          </div>
          {started && <span className="round-badge" title={workingDir}>{shortDir(workingDir)}</span>}
          {started && orchModel && (
            <span className="round-badge model" title={
              `Orchestrator: ${orchModel}${pool.length ? ` · specialist pool: ${pool.join(", ")}` : ""}`
            }>🧠 {orchModel}{pool.length ? ` +${pool.length}` : ""}</span>
          )}
          <span className={"dot " + (connected ? "on" : "off")} title={connected ? "Connected" : "Disconnected"} />
          {running && <button className="btn danger" onClick={stop}>Stop</button>}
        </div>
      </header>

      {!started ? (
        <div className="startwrap">
          <div className="startcard">
            <h1>Start a Science session</h1>
            <p className="muted">
              An orchestrator agent will work with you: it spins up specialists,
              runs jobs on clusters, curates data into FAIR datasets, searches the
              literature, and presents plots and tables you can refine by commenting.
            </p>
            <label>Working directory</label>
            <div className="dir-row">
              <input
                placeholder="Choose a folder, or paste an absolute path…"
                value={workingDir}
                onChange={(e) => setWorkingDir(e.target.value)}
              />
              <button className="btn" type="button" onClick={browseDir}>Browse…</button>
            </div>
            <label>What are you trying to do? (optional — you can also just start chatting)</label>
            <textarea
              rows={4}
              placeholder="e.g. Curate these assay CSVs into one clean dataset and plot IC50 by target…"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
            />
            <ModelPicker
              models={models} orchLabel="Orchestrator model"
              orchModel={orchModel} setOrchModel={setOrchModel}
              pool={pool} setPool={setPool}
            />
            {remotes.length > 0 && (
              <>
                <label>Compute node (optional)</label>
                <select value={remoteId} onChange={(e) => setRemoteId(e.target.value)}>
                  <option value="">Local only</option>
                  {remotes.map((r) => (
                    <option key={r.id} value={r.id}>
                      {(r.name || r.id) + (r.host ? ` (${r.host})` : "")}
                    </option>
                  ))}
                </select>
                <div className="muted" style={{ fontSize: "0.8em", marginTop: 4 }}>
                  Everything runs locally by default. Pick a node and the agent offloads
                  only HEAVY steps (embeddings, large sims) to it — big files stay there,
                  lightweight results (plots, projections) are fetched back and rendered here.
                </div>
              </>
            )}
            <button className="btn primary big" onClick={() => openSession(workingDir, goal)}
              disabled={!connected || !workingDir.trim()}>
              {connected ? "Open session" : "Connecting…"}
            </button>

            {sessions.length > 0 && (
              <div className="sci-recent">
                <div className="sci-recent-h">Recent sessions</div>
                {sessions.slice(0, 8).map((s) => (
                  <div key={s.working_dir} className="sci-recent-row"
                    onClick={() => openSession(s.working_dir)}>
                    {s.running && <span className="sci-run-dot" title="running" />}
                    <div className="sci-recent-main">
                      <div className="sci-recent-title">{s.title}</div>
                      <div className="sci-recent-dir" title={s.working_dir}>{shortDir(s.working_dir)}</div>
                    </div>
                    <span className="sci-recent-when">{fmtWhen(s.updated_at)}</span>
                    <button className="sci-recent-x" title="Remove from history"
                      onClick={(e) => forgetSession(s.working_dir, e)}>×</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="sci-grid">
          <main className="sci-chat" ref={scrollRef}>
            {todos.length > 0 && (
              <PlanStrip todos={todos} open={showPlan} onToggle={() => setShowPlan((v) => !v)} />
            )}
            {remoteRunning && (
              <div className="sci-banner">
                ⏳ This session is still working (started in another tab/refresh). Live updates
                aren't streamed here.
                <button className="btn" onClick={() => openSession(workingDir)}>Reload state</button>
              </div>
            )}
            {feed.length === 0 && !live && (
              <div className="sci-empty">
                <div className="sci-empty-h">👋 Tell me what you're working on</div>
                <div className="muted">
                  I'll look around your working directory and get to work — running
                  analyses, spinning up specialists, and submitting cluster jobs as needed.
                  Plots, tables, and reports I make appear right here in the chat and in the
                  <b> Outputs</b> panel; <b>comment on any of them</b> to refine.
                </div>
                <div className="sci-empty-egs">
                  {["Inspect the CSVs here and summarise what's in them",
                    "Plot the dose–response curves and flag outliers",
                    "Curate these messy assay files into one clean dataset"].map((eg) => (
                    <button key={eg} className="sci-eg" onClick={() => { setInput(eg); }}>{eg}</button>
                  ))}
                </div>
              </div>
            )}
            {feed.map((it) =>
              it.t === "art" ? (
                <ArtifactCard key={it.id} art={it.art} workingDir={workingDir}
                  onComment={comment} running={running} />
              ) : it.role === "note" ? (
                <div key={it.id} className={"sci-note " + (it.tone || "") + (it.who ? " byspec" : "")}>
                  {it.who && <span className="sci-note-who">{it.icon} {it.who}</span>}
                  {it.text}
                </div>
              ) : (
                <ChatBubble key={it.id} role={it.role} text={it.text} tone={it.tone}
                  who={it.who} icon={it.icon} />
              )
            )}
            {live && <ChatBubble role="assistant" text={live} streaming
              who={liveWho.who} icon={liveWho.icon} />}
            {running && !live && !ask && (
              <div className="sci-working">
                ● {liveWho.who ? `${liveWho.icon} ${liveWho.who}` : "Orchestrator"} working…
              </div>
            )}
            {ask && <AskCard ask={ask} onAnswer={answerQuestion} />}
          </main>

          <aside className="sci-side">
            <div className="sci-tabs">
              <button className={"sci-tab" + (side === "outputs" ? " on" : "")}
                onClick={() => setSide("outputs")}>Outputs ({artifacts.length})</button>
              <button className={"sci-tab" + (side === "team" ? " on" : "")}
                onClick={() => setSide("team")}>Team ({specialists.length})</button>
              <button className={"sci-tab" + (side === "jobs" ? " on" : "")}
                onClick={() => setSide("jobs")}>Jobs ({jobs.length})</button>
              <button className={"sci-tab" + (side === "fair" ? " on" : "")}
                onClick={() => setSide("fair")}>FAIR ({prov.length})</button>
            </div>
            <div className="sci-side-body">
              {side === "outputs" && <OutputsPanel artifacts={artifacts} onPick={focusArtifact} />}
              {side === "team" && <TeamPanel specialists={specialists} />}
              {side === "jobs" && <JobsPanel jobs={jobs} />}
              {side === "fair" && <FairPanel prov={prov} workingDir={workingDir} />}
            </div>
          </aside>

          <footer className="sci-inputbar">
            <input
              className="inject-input"
              placeholder={ask
                ? "Answer the question above to let the turn continue…"
                : remoteRunning
                ? "Session is busy in another tab — reload state to continue…"
                : "Message the orchestrator… (e.g. 'now compare against the AlphaFold model')"}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onSend()}
              disabled={running || remoteRunning}
            />
            <button className="btn primary" onClick={onSend}
              disabled={running || remoteRunning || !input.trim()}>
              Send
            </button>
          </footer>
        </div>
      )}
    </div>
  );
}

function shortDir(d: string): string {
  const parts = d.replace(/\/$/, "").split("/");
  return parts.slice(-2).join("/") || d;
}

function fmtWhen(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function SessionMenu({ sessions, current, onOpen, onNew, onForget }: {
  sessions: SessionMeta[];
  current: string;
  onOpen: (dir: string) => void;
  onNew: () => void;
  onForget: (dir: string, e: React.MouseEvent) => void;
}) {
  return (
    <div className="sci-menu">
      <button className="sci-menu-new" onClick={onNew}>＋ New session</button>
      <div className="sci-menu-list">
        {sessions.length === 0 && <div className="muted sci-menu-empty">No sessions yet.</div>}
        {sessions.map((s) => (
          <div key={s.working_dir}
            className={"sci-menu-row" + (s.working_dir === current ? " on" : "")}
            onClick={() => onOpen(s.working_dir)}>
            {s.running
              ? <span className="sci-run-dot" title="running" />
              : <span className="sci-run-dot idle" />}
            <div className="sci-menu-main">
              <div className="sci-menu-title">{s.title}</div>
              <div className="sci-menu-dir" title={s.working_dir}>{shortDir(s.working_dir)}</div>
            </div>
            <span className="sci-menu-when">{fmtWhen(s.updated_at)}</span>
            <button className="sci-recent-x" title="Remove from history"
              onClick={(e) => onForget(s.working_dir, e)}>×</button>
          </div>
        ))}
      </div>
    </div>
  );
}

function ChatBubble({ role, text, tone, streaming, who, icon }:
  { role: Role; text: string; tone?: string; streaming?: boolean } & Speaker) {
  const label = role === "user" ? "You" : who ? `${icon || "🔬"} ${who}` : "Orchestrator";
  return (
    <div className={"sci-msg " + role + (tone ? " " + tone : "") + (who ? " byspec" : "")}>
      <div className="sci-msg-who">{label}</div>
      <div className="sci-msg-text">{text}{streaming && <span className="sci-caret">▋</span>}</div>
    </div>
  );
}

const viewUrl = (absPath: string) => `/api/files/view/${encodeURIComponent(absPath)}`;
// Cache-bust a preview so an updated (refined) file re-fetches instead of showing
// the browser's cached copy.
const artUrl = (a: Artifact) => `${viewUrl(a.path)}?v=${a.ver || 1}`;

const TOOL_ICONS: Record<string, string> = {
  read_file: "📄", write_file: "✍️", edit_file: "✏️", bash: "⌘", glob: "🔎",
  grep: "🔎", list_dir: "📁", web_search: "🌐", web_fetch: "🌐",
  bio_inspect: "🧬", present_output: "🖼", record_provenance: "📎",
  curate_dataset: "🗂", literature_search: "📚", spawn_specialist: "🔬",
  continue_specialist: "🔁", ask_user: "❓", remember: "🧠",
  submit_cluster_job: "🖥", check_cluster_job: "🖥", todo_write: "✅",
};
function toolIcon(name: string): string {
  return TOOL_ICONS[name] || "→";
}

// Pick a renderer from the file extension (robust — the emitted `kind` can be
// generic), falling back to the server-provided kind.
function mediaType(rel: string, kind: string): "image" | "table" | "report" | "text" {
  const ext = (rel.split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"].includes(ext)) return "image";
  if (["csv", "tsv"].includes(ext)) return "table";
  if (["html", "htm", "pdf"].includes(ext)) return "report";
  if (kind === "image") return "image";
  return "text";
}

function PlanStrip({ todos, open, onToggle }:
  { todos: { content: string; status: string }[]; open: boolean; onToggle: () => void }) {
  const done = todos.filter((t) => t.status === "completed").length;
  const active = todos.find((t) => t.status === "in_progress");
  return (
    <div className="sci-plan">
      <button className="sci-plan-head" onClick={onToggle}>
        <span className="sci-plan-title">✅ Plan · {done}/{todos.length} done</span>
        {!open && active && <span className="sci-plan-active">▸ {active.content}</span>}
        <span className="sci-plan-chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ul className="sci-todo">
          {todos.map((t, i) => (
            <li key={i} className={"sci-todo-item " + t.status}>
              <span className="sci-todo-mark">
                {t.status === "completed" ? "✓" : t.status === "in_progress" ? "▸" : "○"}
              </span>
              <span>{t.content}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TablePreview({ url }: { url: string }) {
  const [rows, setRows] = useState<string[][]>([]);
  const [more, setMore] = useState(false);
  useEffect(() => {
    let alive = true;
    fetch(url).then((r) => r.text()).then((text) => {
      if (!alive) return;
      const sep = text.indexOf("\t") >= 0 && text.indexOf(",") < 0 ? "\t" : ",";
      const lines = text.split(/\r?\n/).filter((l) => l.length);
      setMore(lines.length > 26);
      setRows(lines.slice(0, 26).map((l) => l.split(sep).slice(0, 15)));
    }).catch(() => { /* ignore */ });
    return () => { alive = false; };
  }, [url]);
  if (!rows.length) return <div className="sci-art-loading">loading preview…</div>;
  const [head, ...body] = rows;
  return (
    <div className="sci-table-wrap">
      <table className="sci-table">
        <thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {more && <div className="sci-table-more">…first 25 rows · open to see all</div>}
    </div>
  );
}

function ArtifactCard({ art, workingDir, onComment, running }:
  { art: Artifact; workingDir: string; onComment: (id: string, t: string) => void; running: boolean }) {
  const [c, setC] = useState("");
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState("");
  const [saved, setSaved] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [imgFailed, setImgFailed] = useState(false);
  const [bust, setBust] = useState(0);   // local refresh after an inline save
  const editable = EDITABLE.test(art.rel);
  const media = mediaType(art.rel, art.kind);
  const url = artUrl(art) + (bust ? `&r=${bust}` : "");
  // A refinement bumps art.ver → url changes → clear a stale load-error.
  useEffect(() => { setImgFailed(false); }, [url]);

  async function startEdit() {
    try {
      const r = await fetch(url);
      setContent(await r.text());
      setEditing(true);
    } catch { /* ignore */ }
  }
  async function save() {
    try {
      const r = await fetch("/api/science/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: art.path, content }),
      });
      const d = await r.json();
      setSaved(d.ok ? "saved ✓" : d.error || "save failed");
      if (d.ok) { setEditing(false); setBust((b) => b + 1); }
    } catch { setSaved("save failed"); }
  }

  return (
    <div className="sci-art" id={"sciart-" + art.id}>
      <div className="sci-art-head">
        <span className="sci-art-kind">{art.kind}</span>
        <span className="sci-art-name" title={art.path}>{art.caption || art.rel}</span>
        {(media === "report" || media === "image") && (
          <button className="sci-art-expand" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "collapse" : "expand"}
          </button>
        )}
        <a className="sci-art-open" href={url} target="_blank" rel="noreferrer">open ↗</a>
      </div>

      {editing ? (
        <div className="sci-art-edit">
          <textarea value={content} onChange={(e) => setContent(e.target.value)} rows={16} />
          <div className="sci-art-editbtns">
            <button className="btn primary" onClick={save}>Save</button>
            <button className="btn" onClick={() => setEditing(false)}>Cancel</button>
            {saved && <span className="muted">{saved}</span>}
          </div>
        </div>
      ) : media === "image" ? (
        imgFailed ? (
          <div className="sci-art-imgfail">
            Couldn't render preview — <a href={url} target="_blank" rel="noreferrer">open the image ↗</a>
          </div>
        ) : (
          <a href={url} target="_blank" rel="noreferrer" className="sci-art-imgwrap">
            <img className={"sci-art-img" + (expanded ? " expanded" : "")}
              src={url} alt={art.caption || art.rel}
              onError={() => setImgFailed(true)} />
          </a>
        )
      ) : media === "table" ? (
        <TablePreview url={url} />
      ) : (
        <iframe className={"sci-art-frame" + (expanded ? " expanded" : "")}
          src={url} title={art.rel} />
      )}

      {art.provenance && <div className="sci-art-prov">📎 {art.provenance}</div>}

      <div className="sci-art-actions">
        {editable && !editing && media !== "image" && (
          <button className="btn" onClick={startEdit}>Edit</button>
        )}
        <input
          className="sci-art-comment"
          placeholder="Comment to refine this output…"
          value={c}
          onChange={(e) => setC(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { onComment(art.id, c); setC(""); } }}
          disabled={running}
        />
        <button className="btn primary" disabled={running || !c.trim()}
          onClick={() => { onComment(art.id, c); setC(""); }}>
          Refine
        </button>
      </div>
    </div>
  );
}

function kindIcon(kind: string): string {
  return { image: "🖼", table: "📊", report: "📄", dataset: "🗂", text: "📄" }[kind] || "📎";
}

function OutputsPanel({ artifacts, onPick }:
  { artifacts: Artifact[]; onPick: (id: string) => void }) {
  if (!artifacts.length)
    return (
      <div className="muted sci-out-empty">
        Plots, tables, and reports the orchestrator produces show up here — and inline
        in the chat, where you can comment to refine them. Click one to jump to it.
      </div>
    );
  return (
    <div className="sci-gallery">
      {artifacts.slice().reverse().map((a) => (
        <button key={a.id} className="sci-gitem" onClick={() => onPick(a.id)} title={a.rel}>
          {mediaType(a.rel, a.kind) === "image" ? (
            <img className="sci-gthumb" src={artUrl(a)} alt={a.rel} />
          ) : (
            <span className="sci-gicon">{kindIcon(a.kind)}</span>
          )}
          <span className="sci-gmeta">
            <span className="sci-gname">{a.caption || a.rel}</span>
            <span className="sci-gkind">{a.kind}</span>
          </span>
          <a className="sci-gopen" href={artUrl(a)} target="_blank" rel="noreferrer"
            onClick={(e) => e.stopPropagation()}>↗</a>
        </button>
      ))}
    </div>
  );
}

function TeamPanel({ specialists }: { specialists: Specialist[] }) {
  if (!specialists.length)
    return <div className="muted">No specialists yet. The orchestrator spins them up as needed.</div>;
  return (
    <div className="roster">
      {specialists.map((a) => (
        <div key={a.id} className={"agent-card " + a.status}>
          <div className="agent-head">
            <span className="agent-icon">{a.icon}</span>
            <span className="agent-name">{a.name}</span>
            <span className={"agent-status " + a.status}>{a.status}</span>
          </div>
          <div className="agent-role">{a.role}</div>
          {a.goal && <div className="agent-goal">{a.goal}</div>}
          {a.summary && <div className="sci-spec-summary">{a.summary}</div>}
          <div className="agent-tools">
            {(a.tools || []).map((t) => <span key={t} className="tool-chip">{t}</span>)}
          </div>
        </div>
      ))}
    </div>
  );
}

function JobsPanel({ jobs }: { jobs: Job[] }) {
  if (!jobs.length)
    return <div className="muted">No jobs submitted. Long computations run on the cluster or in the background.</div>;
  return (
    <div className="sci-jobs">
      {jobs.map((j) => (
        <div key={j.id} className={"sci-job " + j.status}>
          <div className="sci-job-head">
            <span className="sci-job-name">{j.name}</span>
            <span className={"sci-job-status " + j.status}>{j.status}</span>
          </div>
          <div className="sci-job-meta">
            {j.remote_label} · {j.scheduler}{j.handle ? ` · #${j.handle}` : ""}
          </div>
          {j.output && <pre className="sci-job-out">{j.output}</pre>}
        </div>
      ))}
    </div>
  );
}

function FairPanel({ prov, workingDir }: { prov: Prov[]; workingDir: string }) {
  const ledger = viewUrl(workingDir.replace(/\/$/, "") + "/science/PROVENANCE.md");
  return (
    <div className="sci-fair">
      <a className="report-link" href={ledger} target="_blank" rel="noreferrer">
        📖 Open provenance ledger
      </a>
      {prov.length === 0 ? (
        <div className="muted">No provenance recorded yet.</div>
      ) : (
        prov.map((p, i) => (
          <div key={i} className="sci-prov">
            <div className="sci-prov-name">{p.artifact}</div>
            {p.method && <div className="sci-prov-line"><b>method:</b> {p.method}</div>}
            {p.inputs && <div className="sci-prov-line"><b>inputs:</b> {p.inputs}</div>}
            {p.notes && <div className="sci-prov-line muted">{p.notes}</div>}
          </div>
        ))
      )}
    </div>
  );
}
