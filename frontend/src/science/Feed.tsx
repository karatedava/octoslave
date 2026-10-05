import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Markdown } from "./Markdown";
import { FileView } from "./Viewers";
import type { Artifact, Item, SciState, Specialist, Step, Todo } from "./store";
import { artUrl, fmtDur, kindIcon, toolIcon } from "./util";

export function Chev({ open }: { open: boolean }) {
  return <span className={"sx-chev" + (open ? " open" : "")} aria-hidden>›</span>;
}

export function useNow(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [active, ms]);
  return now;
}

type FeedProps = {
  st: SciState;
  onOpenArt: (id: string) => void;
  onRefine: (id: string) => void;
  onRefinePlan: (todos: Todo[]) => void;
  onExample: (text: string) => void;
  focusSpec: string | null;
};

export function Feed({ st, onOpenArt, onRefine, onRefinePlan, onExample, focusSpec }: FeedProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [away, setAway] = useState(false);

  // Stick to the bottom only while the user is there — reading back through
  // the conversation must not be yanked away by every token.
  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
    stick.current = atBottom;
    setAway(!atBottom);
  };
  const toBottom = (smooth = false) => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  };
  useLayoutEffect(() => { if (stick.current) toBottom(); });
  useEffect(() => {
    // Images and tables grow after they load; keep following if we were.
    const el = inner.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => { if (stick.current) toBottom(); });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    if (!focusSpec) return;
    const el = document.getElementById("sxspec-" + focusSpec);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("sx-flash");
      setTimeout(() => el.classList.remove("sx-flash"), 1400);
    }
  }, [focusSpec]);

  const now = useNow(st.running);
  const lastStepsId = [...st.items].reverse().find((i) => i.k === "steps")?.id;

  return (
    <div className="sx-feed" ref={scroller} onScroll={onScroll}>
      <div className="sx-feed-inner" ref={inner}>
        {st.items.length === 0 && !st.live && <Welcome onExample={onExample} />}
        {st.items.map((it) => (
          <FeedItem key={it.id} it={it} st={st} now={now} onOpenArt={onOpenArt}
            onRefine={onRefine} onRefinePlan={onRefinePlan}
            liveSteps={st.running && it.id === lastStepsId} />
        ))}
        {st.live && st.live.text && (
          <div className="sx-msg assistant streaming">
            <Who who={st.live.who} icon={st.live.icon} />
            <Markdown text={st.live.text} />
            <span className="sx-caret" />
          </div>
        )}
      </div>
      {away && (
        <button className="sx-jump" onClick={() => { stick.current = true; setAway(false); toBottom(true); }}>
          ↓ Latest
        </button>
      )}
    </div>
  );
}

function Welcome({ onExample }: { onExample: (t: string) => void }) {
  const egs = [
    "Look through the data in this folder and tell me what's there",
    "Plot the dose–response curves and flag outliers",
    "Curate these messy assay files into one clean, documented dataset",
    "Find recent literature on this target and summarise the open questions",
  ];
  return (
    <div className="sx-welcome">
      <div className="sx-welcome-mark">⌬</div>
      <h2>What are we working on?</h2>
      <p>
        Describe the research goal. The orchestrator plans it, spins up specialists,
        runs the compute, and shows results in the workspace as they appear — and you
        can keep talking to it the whole time to steer.
      </p>
      <div className="sx-egs">
        {egs.map((e) => <button key={e} className="sx-eg" onClick={() => onExample(e)}>{e}</button>)}
      </div>
    </div>
  );
}

function Who({ who, icon }: { who?: string; icon?: string }) {
  return (
    <div className="sx-who">
      {who ? <><span className="sx-who-ic">{icon || "🔬"}</span>{who}</> : <><span className="sx-who-ic orch">◆</span>Orchestrator</>}
    </div>
  );
}

function FeedItem({ it, st, now, onOpenArt, onRefine, onRefinePlan, liveSteps }: {
  it: Item; st: SciState; now: number; onOpenArt: (id: string) => void;
  onRefine: (id: string) => void; onRefinePlan: (todos: Todo[]) => void;
  liveSteps: boolean;
}) {
  switch (it.k) {
    case "user":
      return <UserMsg it={it} art={it.artifactId ? st.artifacts[it.artifactId] : undefined} />;
    case "assistant":
      return (
        <div className={"sx-msg assistant" + (it.tone ? " " + it.tone : "") + (it.who ? " byspec" : "")}>
          <Who who={it.who} icon={it.icon} />
          <Markdown text={it.text} />
        </div>
      );
    case "plan":
      return <PlanCard todos={it.todos} revised={it.revised}
        onRefine={() => onRefinePlan(it.todos)} />;
    case "steps":
      return <StepsGroup steps={it.steps} live={liveSteps} now={now} />;
    case "spec": {
      const sp = st.specialists[it.specId];
      return sp ? <SpecCard sp={sp} runIdx={it.run} now={now} /> : null;
    }
    case "art": {
      const a = st.artifacts[it.artId];
      return a ? <ArtCard a={a} onOpen={() => onOpenArt(a.id)} onRefine={() => onRefine(a.id)} /> : null;
    }
    case "note":
      return (
        <div className={"sx-note " + (it.tone || "")}>
          {it.who && <span className="sx-note-who">{it.icon} {it.who}</span>}
          <span className="sx-note-text">{it.text}</span>
        </div>
      );
  }
}

const STEER_LABEL: Record<string, string> = {
  sending: "sending…",
  queued: "queued — the orchestrator picks this up at its next step",
  delivered: "picked up",
  dropped: "not delivered — the turn was stopped first",
};

function UserMsg({ it, art }: { it: Extract<Item, { k: "user" }>; art?: Artifact }) {
  return (
    <div className={"sx-msg user" + (it.steer ? " steer " + it.steer : "")}>
      {art && <div className="sx-msg-ctx">↳ on <b>{art.caption || art.rel}</b></div>}
      <div className="sx-user-text">{it.text}</div>
      {it.steer && (
        <div className="sx-steer">
          <span className={"sx-steer-dot " + it.steer} />
          {it.steer === "seen" ? `seen by ${it.seenBy || "the specialist"} · reaches the orchestrator when it reports back`
                               : STEER_LABEL[it.steer]}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function StepsGroup({ steps, live, now }: { steps: Step[]; live: boolean; now: number }) {
  const [open, setOpen] = useState(false);
  const running = steps.find((s) => s.ok === undefined);
  const failed = steps.filter((s) => s.ok === false).length;
  const first = steps[0]?.at || now;
  const last = steps[steps.length - 1];
  const dur = (running ? now : (last?.end || last?.at || first)) - first;
  const names = Array.from(new Set(steps.map((s) => s.name))).slice(0, 4);
  return (
    <div className={"sx-steps" + (open ? " open" : "") + (running ? " running" : "")}>
      <button className="sx-steps-head" onClick={() => setOpen((o) => !o)}>
        <Chev open={open} />
        {running && live ? <span className="sx-spin" /> : <span className="sx-steps-ic">{failed ? "⚠" : "✓"}</span>}
        <span className="sx-steps-title">
          {running && live ? running.summary : `${steps.length} step${steps.length === 1 ? "" : "s"}`}
        </span>
        {!(running && live) && <span className="sx-steps-names">{names.map((n) => toolIcon(n) + " " + n).join(" · ")}</span>}
        {running && live && steps.length > 1 && <span className="sx-steps-names">step {steps.length}</span>}
        <span className="sx-steps-dur">{dur > 800 ? fmtDur(dur) : ""}</span>
      </button>
      {open && (
        <div className="sx-steps-list">
          {steps.map((s, i) => <StepRow key={i} s={s} now={now} />)}
        </div>
      )}
    </div>
  );
}

function StepRow({ s, now }: { s: Step; now: number }) {
  const [open, setOpen] = useState(false);
  const detail = stepDetail(s);
  const dur = (s.end || (s.ok === undefined ? now : s.at)) - s.at;
  return (
    <div className={"sx-step" + (s.ok === false ? " bad" : "")}>
      <button className="sx-step-row" onClick={() => detail && setOpen((o) => !o)} disabled={!detail}>
        <span className="sx-step-ic">{toolIcon(s.name)}</span>
        <span className="sx-step-sum">{s.summary}</span>
        <span className="sx-step-st">
          {s.ok === undefined ? <span className="sx-spin sm" /> : s.ok ? "" : "failed"}
          {dur > 1500 ? " " + fmtDur(dur) : ""}
        </span>
      </button>
      {open && detail}
    </div>
  );
}

function stepDetail(s: Step) {
  const a = s.args || {};
  const parts: JSX.Element[] = [];
  if (a.command) parts.push(<pre key="c" className="sx-code">$ {a.command}</pre>);
  if (a.content) parts.push(<pre key="w" className="sx-code">{a.content}</pre>);
  if (a.old_string !== undefined) {
    parts.push(<pre key="o" className="sx-code del">{a.old_string}</pre>);
    parts.push(<pre key="n" className="sx-code add">{a.new_string}</pre>);
  }
  if (s.preview) parts.push(<pre key="r" className={"sx-code result" + (s.ok === false ? " bad" : "")}>{s.preview}</pre>);
  return parts.length ? <div className="sx-step-detail">{parts}</div> : null;
}

// ---------------------------------------------------------------------------

function SpecCard({ sp, runIdx, now }: { sp: Specialist; runIdx: number; now: number }) {
  const run = sp.runs[runIdx];
  const working = run?.status === "working";
  const [open, setOpen] = useState(false);
  if (!run) return null;
  const dur = (run.ended || (working ? now : run.started)) - run.started;
  const current = [...run.steps].reverse().find((s) => s.ok === undefined);
  const lastSaid = run.live || run.said[run.said.length - 1] || "";
  return (
    <div className={"sx-spec " + run.status} id={runIdx === sp.runs.length - 1 ? "sxspec-" + sp.id : undefined}>
      <div className="sx-spec-head">
        <span className="sx-spec-ic">{sp.icon}</span>
        <div className="sx-spec-id">
          <div className="sx-spec-name">{sp.name}{run.resumed && <span className="sx-tag">resumed</span>}</div>
          <div className="sx-spec-role">{sp.role}{sp.model ? ` · ${sp.model}` : ""}</div>
        </div>
        <span className={"sx-pill " + run.status}>
          {working ? <><span className="sx-spin sm" /> working</> : run.status === "failed" ? "failed" : "done"}
        </span>
        <span className="sx-spec-dur">{fmtDur(dur)}</span>
      </div>
      {sp.goal && runIdx === 0 && <div className="sx-spec-goal">{sp.goal}</div>}
      {working ? (
        <div className="sx-spec-live">
          {current ? (
            <div className="sx-spec-now"><span className="sx-step-ic">{toolIcon(current.name)}</span>{current.summary}</div>
          ) : run.thinking > 0 ? (
            <div className="sx-spec-now muted">thinking… {run.thinking.toLocaleString()} chars</div>
          ) : run.live ? null : (
            <div className="sx-spec-now muted">working…</div>
          )}
          {lastSaid && <div className="sx-spec-said">{tail(lastSaid, 420)}</div>}
        </div>
      ) : run.summary ? (
        <details className="sx-spec-sum" open={run.summary.length < 600}>
          <summary>Report</summary>
          <Markdown text={run.summary} />
        </details>
      ) : null}
      {run.steps.length > 0 && (
        <>
          <button className="sx-spec-toggle" onClick={() => setOpen((o) => !o)}>
            <Chev open={open} /> {run.steps.length} step{run.steps.length === 1 ? "" : "s"}
            {run.steps.some((s) => s.ok === false) ? " · some failed" : ""}
          </button>
          {open && <div className="sx-steps-list">{run.steps.map((s, i) => <StepRow key={i} s={s} now={now} />)}</div>}
        </>
      )}
    </div>
  );
}

function tail(t: string, n: number): string {
  return t.length > n ? "…" + t.slice(-n) : t;
}

// ---------------------------------------------------------------------------

function ArtCard({ a, onOpen, onRefine }: { a: Artifact; onOpen: () => void; onRefine: () => void }) {
  return (
    <div className={"sx-art" + (a.interim ? " interim" : "")} id={"sxart-" + a.id}>
      <div className="sx-art-head">
        <span className="sx-art-ic">{kindIcon(a.kind, a.rel)}</span>
        <div className="sx-art-title">
          <div className="sx-art-cap">{a.caption || a.rel}</div>
          <div className="sx-art-meta">
            {a.rel}
            {a.by && <> · by {a.by}</>}
          </div>
        </div>
        {a.interim && <span className="sx-tag warn" title="An early look — the work is still in progress">preview</span>}
        {a.ver > 1 && <span className="sx-tag" title="Updated in place">v{a.ver}</span>}
      </div>
      <div className="sx-art-body" onClick={onOpen} title="Open in the workspace">
        <FileView url={artUrl(a)} rel={a.rel} kind={a.kind} size="compact" />
      </div>
      <div className="sx-art-actions">
        <button className="sx-btn ghost sm" onClick={onOpen}>Open</button>
        <button className="sx-btn ghost sm" onClick={onRefine}>Refine…</button>
        <a className="sx-btn ghost sm" href={artUrl(a)} target="_blank" rel="noreferrer">↗</a>
        {a.provenance && <span className="sx-art-prov" title={a.provenance}>📎 {a.provenance}</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

/** The plan, shown in full where it is made so the researcher can read it and
 *  change it before the work follows it. The pinned strip above the chat tracks
 *  progress through it afterwards. */
function PlanCard({ todos, revised, onRefine }: {
  todos: Todo[]; revised: boolean; onRefine: () => void;
}) {
  return (
    <div className="sx-plancard">
      <div className="sx-plancard-head">
        <span className="sx-plancard-ic">🗺</span>
        <span className="sx-plancard-t">{revised ? "Revised plan" : "Plan"}</span>
        <span className="sx-plancard-n">{todos.length} steps</span>
      </div>
      <ol className="sx-plancard-list">
        {todos.map((t, i) => (
          <li key={i} className={t.status}>{t.content}</li>
        ))}
      </ol>
      <div className="sx-plancard-foot">
        <button className="sx-btn sm" onClick={onRefine}>Edit this plan…</button>
        <span className="sx-plancard-hint">
          Work follows this. Edit it, or just say what to change — it is picked up
          while the work runs.
        </span>
      </div>
    </div>
  );
}

export function PlanStrip({ todos }: { todos: Todo[] }) {
  // Open while nothing has been ticked off yet: a plan that has just been made
  // should be readable without a click. It collapses itself once work starts.
  const [open, setOpen] = useState(false);
  const started = todos.some((t) => t.status !== "pending");
  const [touched, setTouched] = useState(false);
  const show = touched ? open : !started;
  const done = todos.filter((t) => t.status === "completed").length;
  const active = todos.find((t) => t.status === "in_progress");
  const pct = todos.length ? Math.round((done / todos.length) * 100) : 0;
  return (
    <div className={"sx-plan" + (show ? " open" : "")}>
      <button className="sx-plan-head"
        onClick={() => { setTouched(true); setOpen(!show); }}>
        <span className="sx-plan-bar"><span style={{ width: pct + "%" }} /></span>
        <span className="sx-plan-count">Plan {done}/{todos.length}</span>
        <span className="sx-plan-active">{active ? active.content : done === todos.length ? "All steps done" : ""}</span>
        <Chev open={show} />
      </button>
      {show && (
        <ul className="sx-plan-list">
          {todos.map((t, i) => (
            <li key={i} className={t.status}>
              <span className="sx-plan-mark">{t.status === "completed" ? "✓" : t.status === "in_progress" ? "▸" : "○"}</span>
              {t.content}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
