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

/** Same models in the same order. */
function samePool(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((m, i) => m === b[i]);
}

export default function Science() {
  const sock = useMemo(() => new LabSocket(), []);
  const [st, dispatch] = useReducer(reducer, initialState);
  const [connected, setConnected] = useState(false);
  const [started, setStarted] = useState(false);
  const [loading, setLoading] = useState(false);
  // A remembered session whose directory was not found, so the start screen can
  // explain why the researcher was put back here.
  const [gone, setGone] = useState("");
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
  // Bumped whenever the researcher sends something: the conversation then shows
  // it and follows what comes back, wherever they had scrolled to.
  const [sentTick, setSentTick] = useState(0);
  const [fileTick, setFileTick] = useState(0);
  const [showSettings, setShowSettings] = useState(false);

  const [leftOpen, setLeftOpen] = useState(() => lsGet("sx.left", window.innerWidth > 1100 ? "1" : "0") === "1");
  const [rightOpen, setRightOpen] = useState(() => lsGet("sx.right", "1") === "1");
  const [rightW, setRightW] = useState(() => Number(lsGet("sx.rightW", "0")) || Math.round(window.innerWidth * 0.4));

  // Two breakpoints, matching science.css. Below 1180px the sessions rail
  // floats over the conversation (there is no room to dock all three columns);
  // below 980px the workspace floats too, and then it is one panel at a time.
  const [tight, setTight] = useState(() => window.matchMedia("(max-width: 1180px)").matches);
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 980px)").matches);
  useEffect(() => {
    const mqs: [MediaQueryList, (v: boolean) => void][] = [
      [window.matchMedia("(max-width: 1180px)"), setTight],
      [window.matchMedia("(max-width: 980px)"), setNarrow],
    ];
    const offs = mqs.map(([mq, set]) => {
      const on = () => set(mq.matches);
      mq.addEventListener("change", on);
      return () => mq.removeEventListener("change", on);
    });
    return () => offs.forEach((off) => off());
  }, []);
  useEffect(() => { if (narrow && leftOpen && rightOpen) setLeftOpen(false); }, [narrow]);  // eslint-disable-line react-hooks/exhaustive-deps
  // A width dragged out on a wide screen is kept in localStorage, so it comes
  // back on a smaller window and used to overhang it. Pull it back in.
  useEffect(() => {
    const clamp = () => setRightW((w) => Math.min(w, Math.max(320, window.innerWidth - 620)));
    clamp();
    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
  }, []);
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
  // Save the models and compute node onto the session as soon as they are
  // changed, not just when the next message happens to carry them: the job
  // watcher continues a session on its own from the session's own record, so a
  // change made and then left alone would be shown in the header and ignored
  // by the thing that acts on it.
  //
  // Only ever send a CHANGE the researcher made. Opening a session renders
  // before its state arrives, so an unconditional send pushed whatever was on
  // screen — nothing on a fresh load, the previous session's node and model
  // pool when switching sessions — onto the session that was just opened, and
  // the server persisted that. It corrected itself a round-trip later, but
  // anything reading the session meanwhile (the between-turns job watcher,
  // a message sent straight after opening) saw the wrong node.
  const serverCfg = useRef<{ model: string; pool: string[]; remoteId: string } | null>(null);
  useEffect(() => {
    if (!started || !workingDir) return;
    const s = serverCfg.current;
    if (!s) return;                       // the session's own values aren't in yet
    // Send only what changed. Sending all three re-sent whatever this tab still
    // showed for the others, so changing the model in one tab put back the node
    // another tab had just changed.
    const msg: any = { type: "science_settings", working_dir: workingDir };
    if (orchModel && orchModel !== s.model) msg.model = orchModel;
    if (!samePool(pool, s.pool)) msg.specialist_models = pool;
    if (remoteId !== s.remoteId) msg.remote_id = remoteId || null;
    serverCfg.current = { model: orchModel, pool, remoteId };
    if (Object.keys(msg).length > 2) sock.send(msg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [started, workingDir, orchModel, pool, remoteId]);
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
      if (m.type === "science_settings") {
        // The session's settings changed — in another tab, or in this one. Show
        // what the session now holds and record it as the session's, so the
        // settings effect does not push this tab's older values back over it.
        const cur = cfg.current;
        const next = {
          model: typeof m.model === "string" && m.model ? m.model : cur.orchModel,
          pool: Array.isArray(m.specialist_models) ? m.specialist_models : cur.pool,
          remoteId: "remote_id" in m ? (m.remote_id || "") : cur.remoteId,
        };
        serverCfg.current = next;
        if (next.model !== cur.orchModel) setOrchModel(next.model);
        if (!samePool(next.pool, cur.pool)) setPool(next.pool);
        if (next.remoteId !== cur.remoteId) setRemoteId(next.remoteId);
      }
      if (m.type === "science_done") refreshSessions();
      if (m.type === "science_done" || m.type === "science_artifact") setFileTick((t) => t + 1);
      // An error while opening a session has to stop the wait, or the page sits
      // on "Opening session…" for ever with the explanation hidden behind it —
      // which is exactly what an unreadable state.json produced: a dead end.
      if (m.type === "error") {
        setLoading(false);
        resuming.current = false;
      }
      dispatch({ type: "event", ev: m });
    });
    sock.connect();
    const last = lsGet("science.lastDir");
    if (last) openSession(last, "", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onState(m: any) {
    setLoading(false);
    if (m.exists) setGone("");
    if (!m.exists) {
      sid.current = m.sid || "";
      lastSeq.current = 0;
      dispatch({ type: "reset" });
      if (pendingFirst.current) {
        const first = pendingFirst.current;
        pendingFirst.current = "";
        // A session started right here: its first message carries the models
        // and node on screen, so those ARE the session's from now on. Left
        // unset, nothing changed in the settings afterwards was saved until a
        // reload — while the job watcher kept acting on the old values.
        serverCfg.current = { model: cfg.current.orchModel, pool: cfg.current.pool,
                              remoteId: cfg.current.remoteId };
        send(first, null);
      } else if (resuming.current) {
        // The remembered session isn't there. Say so: landing back on the start
        // screen with no explanation reads as the app losing the work, and the
        // usual cause (a moved folder, an unmounted or not-yet-synced drive) is
        // something only the researcher can put right.
        setStarted(false);
        lsSet("science.lastDir", null);
        setGone(m.working_dir || m.sid || "");
      }
      resuming.current = false;
      return;
    }
    // A reconnect re-opens the session already on screen: keep what the
    // researcher has open (and any draft in its editor) instead of jumping away.
    const reopened = !!sid.current && sid.current === (m.sid || m.working_dir);
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
    // What the SESSION holds, so the settings effect above can tell a change the
    // researcher made from the screen simply catching up with the session. For a
    // session being started now, the researcher's choices are authoritative and
    // the first message carries them.
    serverCfg.current = { model: cfg.current.orchModel, pool: cfg.current.pool,
                          remoteId: cfg.current.remoteId };
    dispatch({ type: "hydrate", snapshot: snap, events: evs, running: !!m.running });
    const arts: string[] = (snap.artifacts || []).map((a: any) => a.id);
    if (!reopened) setDoc(arts.length ? { type: "art", id: arts[arts.length - 1] } : null);
    resuming.current = false;
    if (pendingFirst.current) {
      const first = pendingFirst.current;
      pendingFirst.current = "";
      send(first, null);
    }
  }

  function hideArt(id: string) {
    sock.send({ type: "science_hide_artifact", working_dir: cfg.current.workingDir, artifact_id: id });
    // Don't leave the pane on an output that is going away: show the latest other one.
    if (doc && doc.type === "art" && doc.id === id) {
      const rest = st.artOrder.filter((x) => x !== id);
      setDoc(rest.length ? { type: "art", id: rest[rest.length - 1] } : null);
    }
  }

  function openSession(dir: string, firstMsg = "", isResume = false) {
    if (!dir.trim()) return;
    dispatch({ type: "reset" });
    sid.current = "";
    lastSeq.current = 0;
    // Whatever is on screen belongs to the session being left, not this one.
    serverCfg.current = null;
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
    setSentTick((n) => n + 1);
  }

  function answer(text: string) {
    const t = text.trim();
    if (!t || !st.ask) return;
    sock.send({ type: "user_response", answer: t, id: st.ask.id || "" });
    dispatch({ type: "answered", text: t });
    setSentTick((n) => n + 1);
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
    // Measure the columns' own container: the app rail, and the sessions rail
    // when it is docked rather than floating, are not space this panel can take.
    const body = (e.currentTarget as HTMLElement).parentElement;
    const avail = body ? body.clientWidth : window.innerWidth;
    const left = !tight && leftOpen ? 256 : 0;
    const max = Math.max(300, avail - left - 380);
    const move = (ev: MouseEvent) => {
      const w = window.innerWidth - ev.clientX;
      setRightW(Math.max(300, Math.min(w, max)));
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
        <button className={"sx-panel-btn" + (leftOpen ? " on" : "")} aria-pressed={leftOpen}
          title="Sessions & files (⌘B)" onClick={() => openLeft(!leftOpen)}>
          <span className="sx-panel-ic">☰</span>
          <span className="sx-panel-label">Files</span>
        </button>
        <div className="page-title">Science</div>
        {started && (
          <div className="sx-top-session" title={workingDir}>
            <span className="sx-top-task">{task || shortDir(workingDir)}</span>
            {st.running && st.net ? (
              st.net.state === "limited" ? (
                <span className="sx-state wait" title={`The model server (${st.net.host}) is refusing new requests from this key for now (its limit on requests at once is used up). The work carries on by itself once it accepts them again; Stop ends it.`}>
                  <span className="sx-pulse sm" />waiting for a free model slot…
                </span>
              ) : (
                <span className="sx-state wait" title={`The model server (${st.net.host}) cannot be reached — the network is down or the computer just woke up. The work carries on by itself once it is back; Stop ends it.`}>
                  <span className="sx-pulse sm" />waiting for the network…
                </span>
              )
            ) : (
              <span className={"sx-state " + (st.running ? "run" : "idle")}>
                {st.running ? <><span className="sx-pulse sm" />{working ? `${working} specialist${working > 1 ? "s" : ""} working` : "working"}</> : "idle"}
              </span>
            )}
          </div>
        )}
        <span className="sx-spacer" />
        {started && (
          <div className="sx-settings-wrap">
            <button className={"sx-chip model" + (showSettings ? " on" : "")} onClick={() => setShowSettings((v) => !v)}
              aria-expanded={showSettings} aria-haspopup="dialog"
              title="Models & compute for this session">
              {orchModel || "model"}{pool.length ? ` +${pool.length}` : ""}{nodeName ? ` · ${nodeName}` : ""}
            </button>
            {showSettings && (
              <>
                <div className="sx-scrim" onClick={() => setShowSettings(false)} />
                <div className="sx-pop" role="dialog" aria-label="Session settings">
                  <div className="sx-pop-h">Session settings <span className="sx-muted">— saved to this session</span></div>
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
          <button className={"sx-panel-btn" + (rightOpen ? " on" : "")} aria-pressed={rightOpen}
            title={(rightOpen ? "Hide" : "Show") + " the workspace — outputs, team, jobs, provenance (⌘J)"}
            onClick={() => openRight(!rightOpen)}>
            <span className="sx-panel-ic">◨</span>
            <span className="sx-panel-label">Workspace</span>
          </button>
        )}
      </header>

      <div className="sx-body">
        {((tight && leftOpen) || (narrow && started && rightOpen)) && (
          <div className="sx-overlay-scrim"
            onClick={() => { setLeftOpen(false); if (narrow) setRightOpen(false); }} />
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
            onStart={(goal) => openSession(workingDir, goal)} onOpen={(d) => openSession(d)}
            gone={gone} dismissGone={() => setGone("")} />
        ) : (
          <>
            <main className="sx-center">
              {st.todos.length > 0 && <PlanStrip todos={st.todos} />}
              {loading ? (
                <div className="sx-feed"><div className="sx-loading big">Opening session…</div></div>
              ) : (
                <Feed st={st} onOpenArt={openArt} onRefine={startRefine} onHide={hideArt} focusSpec={focusSpec}
                  onRefinePlan={refinePlan} workingDir={workingDir} sentTick={sentTick}
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
                    onHide={hideArt}
                    onAutoContinue={(on) => sock.send({ type: "science_settings",
                      working_dir: cfg.current.workingDir, auto_continue: on })}
                    onFocusSpec={(id) => { setFocusSpec(null); setTimeout(() => setFocusSpec(id), 0); }} />
                </div>
              </>
            )}
          </>
        )}
      </div>

      <footer className="sx-status">
        <span className={"sx-conn " + (connected ? "on" : "off")}>{connected ? "● connected" : "● reconnecting…"}</span>
        {connected && st.net && (st.net.state === "limited"
          ? <span className="sx-conn off" title="The model server refuses this key's requests for now (rate limit). The run waits and continues by itself.">● {st.net.host || "the model server"} is refusing new requests (rate limit) — waiting</span>
          : <span className="sx-conn off" title="The run is waiting for the model server and continues by itself when it is reachable again.">● no connection to {st.net.host || "the model server"} — waiting</span>)}
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
