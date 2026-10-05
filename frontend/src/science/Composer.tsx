import { useEffect, useLayoutEffect, useState, type RefObject } from "react";
import type { Ask } from "../AskCard";
import type { SciState } from "./store";
import { useNow } from "./Feed";
import { fmtDur } from "./util";

export type RefineCtx = { id: string; label: string } | null;

type Props = {
  st: SciState;
  value: string;
  setValue: (v: string) => void;
  inputRef: RefObject<HTMLTextAreaElement>;
  refine: RefineCtx;
  clearRefine: () => void;
  connected: boolean;
  onSend: (text: string) => void;
  onAnswer: (text: string) => void;
  onStop: () => void;
};

export function Composer(p: Props) {
  const { st, value, setValue, inputRef } = p;
  const ask = st.ask;

  // Grow with the text, up to a cap.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = Math.min(el.scrollHeight, 220) + "px";
  }, [value, inputRef]);

  const submit = () => {
    const t = value.trim();
    if (!t) return;
    if (ask) p.onAnswer(t); else p.onSend(t);
    setValue("");
  };

  const placeholder = ask
    ? "Type your answer…"
    : p.refine
    ? `What should change in “${p.refine.label}”?`
    : st.running
    ? "Steer the work — the orchestrator picks this up at its next step…"
    : "Describe a goal, ask a question, or give an instruction…";

  return (
    <div className={"sx-composer" + (st.running ? " running" : "") + (ask ? " asking" : "")}>
      <LiveBar st={st} onStop={p.onStop} />
      {ask && <AskPrompt ask={ask} onAnswer={p.onAnswer} />}
      <div className="sx-input">
        {p.refine && !ask && (
          <div className="sx-ctx">
            <span>Refining</span><b>{p.refine.label}</b>
            <button onClick={p.clearRefine} title="Clear (Esc)">×</button>
          </div>
        )}
        <textarea
          ref={inputRef}
          rows={1}
          value={value}
          placeholder={placeholder}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
            if (e.key === "Escape" && p.refine) p.clearRefine();
          }}
        />
        <div className="sx-input-bar">
          <span className="sx-hint">
            {ask ? "Enter to answer — the paused turn resumes"
              : st.running ? "Enter to steer · agents keep working"
              : "Enter to send · Shift+Enter for a new line"}
          </span>
          <span className="sx-spacer" />
          {st.running && (
            <button className="sx-btn sm danger" onClick={p.onStop} title="Stop the turn (work so far is kept)">
              ■ Stop
            </button>
          )}
          <button className="sx-btn sm primary" disabled={!value.trim() || !p.connected} onClick={submit}>
            {ask ? "Answer" : st.running ? "Steer ↵" : "Send ↵"}
          </button>
        </div>
      </div>
    </div>
  );
}

function LiveBar({ st, onStop }: { st: SciState; onStop: () => void }) {
  const now = useNow(st.running);
  if (!st.running) return null;
  const working = Object.values(st.specialists).filter((s) => s.status === "working");
  const queued = st.items.filter((i) => i.k === "user" && (i.steer === "queued" || i.steer === "seen" || i.steer === "sending")).length;
  const jobs = Object.values(st.jobs).filter((j) => ["submitted", "running"].includes(j.status)).length;
  // Specialists work in the background, so the orchestrator is usually busy at
  // the same time. Keep the line about the orchestrator whenever it is doing
  // something — it is the one the researcher is talking to — and fall back to
  // the specialists only when it is idle, waiting on them. (``st.activity`` is
  // always the orchestrator's own step; a specialist's lives in its run.)
  const orchBusy = !!(st.activity || st.live || st.thinking);

  let who = "Orchestrator";
  let icon = "◆";
  let what = "working…";
  let since = st.turnStarted || now;
  if (st.ask) {
    if (st.ask.who) who = st.ask.who;
    what = "waiting for your answer";
  } else if (orchBusy) {
    if (st.activity) { what = st.activity.label; since = st.activity.since; }
    else if (st.live) what = "writing…";
    else what = `thinking… ${st.thinking.toLocaleString()} chars`;
  } else if (working.length === 1) {
    const spec = working[0];
    const run = spec.runs[spec.runs.length - 1];
    const specStep = run ? [...run.steps].reverse().find((s) => s.ok === undefined) : undefined;
    who = spec.name; icon = spec.icon;
    if (specStep) { what = specStep.summary; since = specStep.at; }
    else if (run?.live) what = "writing…";
    else if (run?.thinking) what = `thinking… ${run.thinking.toLocaleString()} chars`;
    else { what = "working…"; since = run?.started ?? since; }
  } else if (working.length > 1) {
    what = `waiting on ${working.length} specialists`;
  }
  return (
    <div className="sx-livebar">
      <span className="sx-pulse" />
      <span className="sx-live-who">{icon} {who}</span>
      <span className="sx-live-what">{what}</span>
      <span className="sx-live-t">{fmtDur(now - since)}</span>
      <span className="sx-spacer" />
      {queued > 0 && <span className="sx-live-chip">{queued} message{queued === 1 ? "" : "s"} queued</span>}
      {working.length > 0 && (orchBusy || working.length > 1) && (
        <span className="sx-live-chip" title={working.map((s) => s.name).join(", ")}>
          {working.length} specialist{working.length === 1 ? "" : "s"} working
        </span>
      )}
      {jobs > 0 && <span className="sx-live-chip">{jobs} job{jobs === 1 ? "" : "s"} running</span>}
      <span className="sx-live-t" title="Turn time">turn {fmtDur(now - (st.turnStarted || now))}</span>
      <button className="sx-live-stop" onClick={onStop} title="Stop">■</button>
    </div>
  );
}

function AskPrompt({ ask, onAnswer }: { ask: Ask; onAnswer: (t: string) => void }) {
  const [left, setLeft] = useState(() => Math.max(0, ask.expires - Date.now()));
  useEffect(() => {
    const t = window.setInterval(() => setLeft(Math.max(0, ask.expires - Date.now())), 1000);
    return () => clearInterval(t);
  }, [ask.expires]);
  return (
    <div className="sx-ask">
      <div className="sx-ask-head">
        <span>❓ {ask.who ? `${ask.who} is asking` : "The orchestrator is asking"}</span>
        <span className={"sx-ask-clock" + (left < 60000 ? " urgent" : "")}>
          {left > 0 ? `${fmtDur(left)} left` : "timed out — carrying on"}
        </span>
      </div>
      <div className="sx-ask-q">{ask.question}</div>
      {ask.options.length > 0 && (
        <div className="sx-chips">
          {ask.options.map((o) => <button key={o} className="sx-chip" onClick={() => onAnswer(o)}>{o}</button>)}
        </div>
      )}
    </div>
  );
}
