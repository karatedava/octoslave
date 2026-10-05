// OctoSlave Science — an IDE-style workspace for research with an orchestrator.
//
//   sessions + files │ conversation (+ live status, always-on composer) │ workspace
//
// The composer is never locked: while agents work, a message steers the running
// turn (the server queues it and the orchestrator picks it up at its next step).
import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { LabSocket } from "../ws";
import { Composer, type RefineCtx } from "./Composer";
import { Feed, PlanStrip } from "./Feed";
import { ModelPicker } from "../ModelPicker";
import { Sidebar, type SessionMeta } from "./Sidebar";
import { Start, type Remote } from "./Start";
import { initialState, reducer } from "./store";
import { newCid, shortDir } from "./util";
import { Workspace, type Doc, type WsTab } from "./Workspace";
import { AppRail } from "../AppRail";
import "./science.css";

function lsGet(k: string, d = ""): string {
  try { return localStorage.getItem(k) ?? d; } catch { return d; }
}
function lsSet(k: string, v: string | null) {
  try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* ignore */ }
}

export default function Science() {
  const sock = useMemo(() => new LabSocket(), []);
  const [st, dispatch] = useReducer(reducer, initialState);
  const [connected, setConnected] = useState(false);
  const [started, setStarted] = useState(false);
  const [loading, setLoading] = useState(false);
  const [workingDir, setWorkingDir] = useState("");
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [orchModel, setOrchModel] = useState("");
  const [pool, setPool] = useState<string[]>([]);
  const [remotes, setRemotes] = useState<Remote[]>([]);
  const [remoteId, setRemoteId] = useState("");
  const [task, setTask] = useState("");

  const [input, setInput] = useState("");
  const [refine, setRefine] = useState<RefineCtx>(null);
  const [doc, setDoc] = useState<Doc>(null);
  const [wsTab, setWsTab] = useState<WsTab>("preview");
  const [follow, setFollow] = useState(true);
  const [focusSpec, setFocusSpec] = useState<string | null>(null);
  const [fileTick, setFileTick] = useState(0);
  const [showSettings, setShowSettings] = useState(false);

  const [leftOpen, setLeftOpen] = useState(() => lsGet("sx.left", window.innerWidth > 1100 ? "1" : "0") === "1");
  const [rightOpen, setRightOpen] = useState(() => lsGet("sx.right", "1") === "1");
  const [rightW, setRightW] = useState(() => Number(lsGet("sx.rightW", "0")) || Math.round(window.innerWidth * 0.4));

  // Below ~1000px the sidebar and workspace become overlays: one at a time.
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 980px)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 980px)");
    const on = () => setNarrow(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  useEffect(() => { if (narrow && leftOpen && rightOpen) setLeftOpen(false); }, [narrow]);  // eslint-disable-line react-hooks/exhaustive-deps
  const openLeft = (v: boolean) => { setLeftOpen(v); if (v && narrow) setRightOpen(false); };
  const openRight = (v: boolean) => { setRightOpen(v); if (v && narrow) setLeftOpen(false); };

  const inputRef = useRef<HTMLTextAreaElement>(null);
  // The socket handler is registered once, so it reads live values via refs.
  const cfg = useRef({ workingDir: "", orchModel: "", pool: [] as string[], remoteId: "" });
  const sid = useRef("");
  const lastSeq = useRef(0);
  const pendingFirst = useRef("");
  const resuming = useRef(false);
  const opened = useRef(false);
  const runningRef = useRef(false);

  useEffect(() => { cfg.current = { workingDir, orchModel, pool, remoteId }; }, [workingDir, orchModel, pool, remoteId]);
  useEffect(() => { runningRef.current = st.running; }, [st.running]);
  useEffect(() => { lsSet("sx.left", leftOpen ? "1" : "0"); }, [leftOpen]);
  useEffect(() => { lsSet("sx.right", rightOpen ? "1" : "0"); }, [rightOpen]);
  useEffect(() => { lsSet("sx.rightW", String(rightW)); }, [rightW]);
  useEffect(() => {
    document.title = started ? `${task ? task.slice(0, 40) : shortDir(workingDir)} · Science` : "OctoSlave Science";
  }, [started, task, workingDir]);

  // ---- data --------------------------------------------------------------
  async function refreshSessions() {
    try {
      const d = await (await fetch("/api/science/sessions")).json();
      setSessions(d.sessions || []);
    } catch { /* ignore */ }
  }

  useEffect(() => {
    fetch("/api/remotes").then((r) => r.json())
      .then((d) => setRemotes(Array.isArray(d.remotes) ? d.remotes : [])).catch(() => {});
    fetch("/api/models").then((r) => r.json()).then((d) => {
      const ms: string[] = Array.isArray(d.models) ? d.models : [];
      setModels(ms);
      // Only seed a default — never clobber a model restored from the session.
      setOrchModel((cur) => cur || (d.default && ms.includes(d.default) ? d.default : ms[0] || ""));
    }).catch(() => {});
    refreshSessions();
    const t = window.setInterval(refreshSessions, 15000);
    return () => clearInterval(t);
  }, []);

  // ---- socket ------------------------------------------------------------
  useEffect(() => {
    sock.onOpen(() => {
      setConnected(true);
      // A reconnect is a fresh server connection: re-open the session on it.
      // The server replays the timeline and re-attaches us to a running turn.
      if (opened.current && cfg.current.workingDir) {
        sock.send({ type: "science_load", working_dir: cfg.current.workingDir });
      }
      opened.current = true;
    });
    sock.onClose(() => setConnected(false));
    sock.onMessage((m) => {
      if (m.type === "science_state") { onState(m); return; }
      if (m.sid && sid.current && m.sid !== sid.current) return;   // another session's event
      if (typeof m.seq === "number") {
        if (m.seq <= lastSeq.current) return;                      // already in the replayed log
        lastSeq.current = m.seq;
      }
      if (m.type === "science_done") refreshSessions();
      if (m.type === "science_done" || m.type === "science_artifact") setFileTick((t) => t + 1);
      dispatch({ type: "event", ev: m });
    });
    sock.connect();
    const last = lsGet("science.lastDir");
    if (last) openSession(last, "", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onState(m: any) {
    setLoading(false);
    if (!m.exists) {
      sid.current = m.sid || "";
      lastSeq.current = 0;
      dispatch({ type: "reset" });
      if (pendingFirst.current) {
        const first = pendingFirst.current;
        pendingFirst.current = "";
        send(first, null);
      } else if (resuming.current) {
        setStarted(false);          // the remembered session is gone
        lsSet("science.lastDir", null);
      }
      resuming.current = false;
      return;
    }
    sid.current = m.sid || m.working_dir;
    const evs: any[] = m.events || [];
    lastSeq.current = evs.reduce((mx, e) => (typeof e.seq === "number" && e.seq > mx ? e.seq : mx), 0);
    const snap = m.snapshot || {};
    setTask(m.task || snap.task || "");
    // Keep the models/node the session was started with — unless the user just
    // chose some on the start screen for this very message.
    if (!pendingFirst.current) {
      if (snap.model) { setOrchModel(snap.model); cfg.current.orchModel = snap.model; }
      if (Array.isArray(snap.specialist_models)) { setPool(snap.specialist_models); cfg.current.pool = snap.specialist_models; }
      setRemoteId(snap.remote_id || ""); cfg.current.remoteId = snap.remote_id || "";
    }
    dispatch({ type: "hydrate", snapshot: snap, events: evs, running: !!m.running });
    const arts: string[] = (snap.artifacts || []).map((a: any) => a.id);
    setDoc(arts.length ? { type: "art", id: arts[arts.length - 1] } : null);
    resuming.current = false;
    if (pendingFirst.current) {
      const first = pendingFirst.current;
      pendingFirst.current = "";
      send(first, null);
    }
  }

  function openSession(dir: string, firstMsg = "", isResume = false) {
    if (!dir.trim()) return;
    dispatch({ type: "reset" });
    sid.current = "";
    lastSeq.current = 0;
    setWorkingDir(dir);
    cfg.current = { ...cfg.current, workingDir: dir };
    setStarted(true);
    setLoading(true);
    setDoc(null);
    setRefine(null);
    setTask(firstMsg.trim());
    setShowSettings(false);
    lsSet("science.lastDir", dir);
    resuming.current = isResume;
    pendingFirst.current = firstMsg.trim();
    sock.send({ type: "science_load", working_dir: dir });
    refreshSessions();
  }

  function newSession() {
    setStarted(false);
    dispatch({ type: "reset" });
    sid.current = "";
    setTask("");
    setDoc(null);
    lsSet("science.lastDir", null);
  }

  async function forgetSession(dir: string) {
    try { await fetch(`/api/science/sessions?working_dir=${encodeURIComponent(dir)}`, { method: "DELETE" }); } catch { /* ignore */ }
    refreshSessions();
  }

  // ---- actions -----------------------------------------------------------
  function send(text: string, refineCtx: RefineCtx) {
    const t = text.trim();
    if (!t) return;
    const c = cfg.current;
    const cid = newCid();
    dispatch({ type: "local_user", cid, text: t, artifactId: refineCtx?.id, steering: runningRef.current });
    runningRef.current = true;
    if (!task) setTask(t);
    const base = { cid, working_dir: c.workingDir, model: c.orchModel || undefined,
                   specialist_models: c.pool, remote_id: c.remoteId || null };
    sock.send(refineCtx
      ? { type: "science_comment", artifact_id: refineCtx.id, text: t, ...base }
      : { type: "science_message", message: t, ...base });
    setRefine(null);
  }

  function answer(text: string) {
    const t = text.trim();
    if (!t || !st.ask) return;
    sock.send({ type: "user_response", answer: t });
    dispatch({ type: "answered", text: t });
  }

  function stop() {
    sock.send({ type: "science_stop", working_dir: cfg.current.workingDir });
  }

  function startRefine(artId: string) {
    const a = st.artifacts[artId];
    if (!a) return;
    setRefine({ id: a.id, label: a.caption || a.rel });
    setTimeout(() => inputRef.current?.focus(), 0);
  }

  function openArt(id: string) {
    setDoc({ type: "art", id });
    setWsTab("preview");
    openRight(true);
  }

  // Put the plan in the composer as an editable list. Sending it is ordinary
  // steering, so it lands whether or not a turn is already running.
  function refinePlan(todos: { content: string; status: string }[]) {
    const body = todos.map((t, i) => `${i + 1}. ${t.content}`).join("\n");
    setInput(`Use this plan instead (edit the steps as you like):\n${body}\n`);
    setTimeout(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }, 0);
  }

  function mention(rel: string) {
    setInput((v) => (v ? v.replace(/\s*$/, " ") : "") + "`" + rel + "` ");
    setTimeout(() => inputRef.current?.focus(), 0);
  }

  // A newly presented result opens in the workspace (unless the user pinned a
  // different one by turning "follow" off).
  useEffect(() => {
    const la = st.lastArt;
    if (!la || !follow) return;
    setDoc({ type: "art", id: la.id });
    setWsTab("preview");
    // Don't throw an overlay over the conversation on a small screen.
    if (la.isNew && !narrow) setRightOpen(true);
  }, [st.lastArt?.n]);   // eslint-disable-line react-hooks/exhaustive-deps

  // ---- keyboard ----------------------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target as HTMLElement)?.tagName || "");
      if (mod && e.key.toLowerCase() === "b") { e.preventDefault(); openLeft(!leftOpen); }
      else if (mod && e.key.toLowerCase() === "j") { e.preventDefault(); openRight(!rightOpen); }
      else if (e.key === "/" && !typing && started) { e.preventDefault(); inputRef.current?.focus(); }
      else if (e.key === "Escape" && showSettings) setShowSettings(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [started, showSettings, leftOpen, rightOpen, narrow]);   // eslint-disable-line react-hooks/exhaustive-deps

  // ---- resize ------------------------------------------------------------
  function startDrag(e: React.MouseEvent) {
    e.preventDefault();
    const move = (ev: MouseEvent) => {
      const w = window.innerWidth - ev.clientX;
      setRightW(Math.max(320, Math.min(w, window.innerWidth - 460)));
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.classList.remove("sx-dragging");
    };
    document.body.classList.add("sx-dragging");
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  const nodeName = remoteId ? (remotes.find((r) => r.id === remoteId)?.name || remoteId) : "";
  const working = Object.values(st.specialists).filter((s) => s.status === "working").length;
  const activeFile = doc?.type === "file" ? doc.path : doc?.type === "art" ? st.artifacts[doc.id]?.path || "" : "";

  return (
    <>
    <AppRail current="science" connected={connected} />
    <div className="app-page">
    <div className="sx">
      <header className="sx-top">
        <button className="sx-icon-btn" title="Sessions & files (⌘B)" onClick={() => openLeft(!leftOpen)}>☰</button>
        <div className="page-title">Science</div>
        {started && (
          <div className="sx-top-session" title={workingDir}>
            <span className="sx-top-task">{task || shortDir(workingDir)}</span>
            <span className={"sx-state " + (st.running ? "run" : "idle")}>
              {st.running ? <><span className="sx-pulse sm" />{working ? `${working} specialist${working > 1 ? "s" : ""} working` : "working"}</> : "idle"}
            </span>
          </div>
        )}
        <span className="sx-spacer" />
        {started && (
          <div className="sx-settings-wrap">
            <button className={"sx-chip model" + (showSettings ? " on" : "")} onClick={() => setShowSettings((v) => !v)}
              title="Models & compute for this session">
              {orchModel || "model"}{pool.length ? ` +${pool.length}` : ""}{nodeName ? ` · ${nodeName}` : ""}
            </button>
            {showSettings && (
              <>
                <div className="sx-scrim" onClick={() => setShowSettings(false)} />
                <div className="sx-pop">
                  <div className="sx-pop-h">Session settings <span className="sx-muted">— apply from the next message</span></div>
                  <ModelPicker models={models} orchLabel="Orchestrator model" orchModel={orchModel}
                    setOrchModel={setOrchModel} pool={pool} setPool={setPool} />
                  {remotes.length > 0 && (
                    <>
                      <label className="sx-label">Compute node</label>
                      <select className="sx-field" value={remoteId} onChange={(e) => setRemoteId(e.target.value)}>
                        <option value="">Local only</option>
                        {remotes.map((r) => <option key={r.id} value={r.id}>{(r.name || r.id) + (r.host ? ` (${r.host})` : "")}</option>)}
                      </select>
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        )}
        {started && (
          <button className={"sx-icon-btn" + (rightOpen ? " on" : "")} title="Toggle workspace (⌘J)" onClick={() => openRight(!rightOpen)}>◨</button>
        )}
      </header>

      <div className="sx-body">
        {narrow && (leftOpen || (started && rightOpen)) && (
          <div className="sx-overlay-scrim" onClick={() => { setLeftOpen(false); setRightOpen(false); }} />
        )}
        {leftOpen && (
          <Sidebar sessions={sessions} current={started ? workingDir : ""} onOpen={(d) => openSession(d)}
            onNew={newSession} onForget={forgetSession} workingDir={started ? workingDir : ""}
            refreshKey={fileTick} activeFile={activeFile}
            onOpenFile={(path, rel) => { setDoc({ type: "file", path, rel }); setWsTab("preview"); openRight(true); setFollow(false); }} />
        )}

        {!started ? (
          <Start connected={connected} workingDir={workingDir} setWorkingDir={setWorkingDir}
            models={models} orchModel={orchModel} setOrchModel={setOrchModel} pool={pool} setPool={setPool}
            remotes={remotes} remoteId={remoteId} setRemoteId={setRemoteId} sessions={sessions}
            onStart={(goal) => openSession(workingDir, goal)} onOpen={(d) => openSession(d)} />
        ) : (
          <>
            <main className="sx-center">
              {st.todos.length > 0 && <PlanStrip todos={st.todos} />}
              {loading ? (
                <div className="sx-feed"><div className="sx-loading big">Opening session…</div></div>
              ) : (
                <Feed st={st} onOpenArt={openArt} onRefine={startRefine} focusSpec={focusSpec}
                  onRefinePlan={refinePlan}
                  onExample={(t) => { setInput(t); inputRef.current?.focus(); }} />
              )}
              <Composer st={st} value={input} setValue={setInput} inputRef={inputRef}
                refine={refine} clearRefine={() => setRefine(null)} connected={connected}
                onSend={(t) => send(t, refine)} onAnswer={answer} onStop={stop} />
            </main>
            {rightOpen && (
              <>
                <div className="sx-resizer" onMouseDown={startDrag} title="Drag to resize" />
                <div className="sx-right" style={{ width: rightW }}>
                  <Workspace st={st} workingDir={workingDir} tab={wsTab} setTab={setWsTab}
                    doc={doc} setDoc={setDoc} follow={follow} setFollow={setFollow}
                    onRefine={startRefine} onMention={mention}
                    onFocusSpec={(id) => { setFocusSpec(null); setTimeout(() => setFocusSpec(id), 0); }} />
                </div>
              </>
            )}
          </>
        )}
      </div>

      <footer className="sx-status">
        <span className={"sx-conn " + (connected ? "on" : "off")}>{connected ? "● connected" : "● reconnecting…"}</span>
        {started && <span className="sx-status-path" title={workingDir}>{workingDir} 📁</span>}
        {started && <span>{nodeName ? `🖥 ${nodeName}` : "💻 local compute"}</span>}
        <span className="sx-spacer" />
        {started && <span>{st.artOrder.length} outputs · {st.specOrder.length} specialists · {st.jobOrder.length} jobs</span>}
        <span className="sx-keys">⌘B files · ⌘J workspace · / message</span>
      </footer>
    </div>
    </div>
    </>
  );
}
