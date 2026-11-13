import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode,
  type WheelEvent as ReactWheelEvent } from "react";
import { Markdown } from "./Markdown";
import { FileView } from "./Viewers";
import type { Artifact, Digest, Item, SciState, Specialist, Step, Todo } from "./store";
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
  onHide: (id: string) => void;
  onRefinePlan: (todos: Todo[]) => void;
  onExample: (text: string) => void;
  focusSpec: string | null;
  workingDir: string;
  sentTick: number;            // bumped each time the researcher sends something
};

// Within this many px of the end counts as being at the latest; further away
// than OFFER_AT, the way back to it is offered.
const AT_END = 24;
const OFFER_AT = 60;

export function Feed({ st, onOpenArt, onRefine, onHide, onRefinePlan, onExample, focusSpec, workingDir, sentTick }: FeedProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);

  // Following the work: the view stays at the latest while the researcher is
  // there, and lets go the moment THEY go back to read — reading must not be
  // yanked away by every token. Only the reader lets go. The view also moves
  // up by itself whenever something around the conversation gets smaller (the
  // plan folding away, the status line ending with the turn, the message box
  // shrinking after a send): the browser pulls the scroll position up to the
  // new end. That used to be taken for the reader leaving, and the work then
  // ran on out of sight until the reader scrolled down after it.
  const stick = useRef(true);
  const lastTop = useRef(0);
  const upAt = useRef(0);                      // when the reader last asked to go up (wheel, keys)
  const goingTo = useRef(0);                   // until when the view glides to a card the reader picked
  const dragging = useRef(false);              // holding the scrollbar
  const touchY = useRef<number | null>(null);  // a finger on the conversation
  const nextId = useRef(st.nextId);
  const newFrom = useRef(Infinity);            // first item the reader has not seen yet
  const [away, setAway] = useState(false);
  const awayNow = useRef(false);

  const measure = () => {
    const el = scroller.current;
    if (!el) return;
    const a = !stick.current && el.scrollHeight - el.scrollTop - el.clientHeight > OFFER_AT;
    if (a !== awayNow.current) { awayNow.current = a; setAway(a); }
  };
  const letGo = () => {
    if (stick.current) { stick.current = false; newFrom.current = nextId.current; }
  };
  const follow = () => { stick.current = true; newFrom.current = Infinity; };
  const toBottom = () => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const top = el.scrollTop;
    const gap = el.scrollHeight - top - el.clientHeight;
    const up = top < lastTop.current - 1;
    lastTop.current = top;
    const now = Date.now();
    const asked = dragging.current || touchY.current !== null || now - upAt.current < 600 || now < goingTo.current;
    // Going up and landing short of the end is the reader (or find-in-page, or
    // a link); going up and landing exactly AT the end is the page getting
    // shorter beneath the view — keep following then. (A smooth scroll starts
    // with a step that small too, hence the explicit signals in ``asked``.)
    if (up && (asked || gap > 2)) letGo();
    else if (gap < AT_END) follow();
    measure();
  };
  // Going up inside a box of its own (a long log, a wide table) scrolls that
  // box, not the conversation — that is not the reader leaving the latest.
  const innerTakesIt = (t: EventTarget | null) => {
    for (let n = t as HTMLElement | null; n && n !== scroller.current; n = n.parentElement) {
      if (n.scrollTop > 0 && n.scrollHeight > n.clientHeight
          && /^(auto|scroll)$/.test(getComputedStyle(n).overflowY)) return true;
    }
    return false;
  };
  const onWheel = (e: ReactWheelEvent) => {
    if (e.ctrlKey || e.deltaY >= 0 || innerTakesIt(e.target)) return;   // ctrl+wheel is a pinch-zoom
    upAt.current = Date.now();
    // Let go now, not at the scroll event: a token arriving in between would
    // otherwise put the view back at the end and swallow a slow trackpad scroll.
    if ((scroller.current?.scrollTop || 0) > 0) letGo();
  };
  // Straight to the latest, and follow it from there.
  const jump = () => {
    goingTo.current = 0;
    follow(); toBottom(); measure();
  };

  // After every render: stay at the latest while following, and check again
  // whether to offer the way back — content grows without any scrolling.
  useLayoutEffect(() => {
    nextId.current = st.nextId;
    if (stick.current && !dragging.current) toBottom();
    measure();
  });
  // Sending something: show it go out and follow what comes back, wherever the
  // reader had scrolled to.
  useLayoutEffect(() => {
    if (sentTick) jump();
  }, [sentTick]);
  useEffect(() => {
    // Content grows after it loads (images, tables), and the view itself
    // changes size when the plan, the status line or the message box do.
    const el = scroller.current, content = inner.current;
    if (!el || !content || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (stick.current && !dragging.current) toBottom();
      measure();
    });
    ro.observe(content);
    ro.observe(el);
    // The reader going up by keyboard (focus in the conversation, not in a field).
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && t !== document.body && !el.contains(t)) return;
      if (t && t.closest("input, textarea, select, [contenteditable]")) return;
      if (e.key === "PageUp" || e.key === "ArrowUp" || e.key === "Home" || (e.key === " " && e.shiftKey))
        upAt.current = Date.now();
    };
    const release = () => { dragging.current = false; };
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    window.addEventListener("blur", release);
    return () => {
      ro.disconnect();
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
      window.removeEventListener("blur", release);
    };
  }, []);
  useEffect(() => {
    if (!focusSpec) return;
    const el = document.getElementById("sxspec-" + focusSpec);
    if (el) {
      // The reader asked to look at this card: do not pull them off it, nor
      // take the glide's first small step for the page getting shorter.
      letGo();
      goingTo.current = Date.now() + 1500;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("sx-flash");
      setTimeout(() => el.classList.remove("sx-flash"), 1400);
    }
  }, [focusSpec]);

  // What arrived since the reader left the latest; an answer still being
  // written counts too (its end has not been seen).
  let fresh = 0;
  if (away) {
    for (let i = st.items.length - 1; i >= 0 && st.items[i].id >= newFrom.current; i--) fresh++;
    if (st.live && st.live.text) fresh++;
  }

  const now = useNow(st.running);
  const lastStepsId = [...st.items].reverse().find((i) => i.k === "steps")?.id;
  // Stable callbacks, so a memoised item re-renders only when ITS data changes
  // — not on every streamed token (a long session has hundreds of items, each
  // re-parsing its Markdown).
  const cbs = useRef({ onOpenArt, onRefine, onHide, onRefinePlan });
  cbs.current = { onOpenArt, onRefine, onHide, onRefinePlan };
  const stable = useMemo(() => ({
    onOpenArt: (id: string) => cbs.current.onOpenArt(id),
    onRefine: (id: string) => cbs.current.onRefine(id),
    onHide: (id: string) => cbs.current.onHide(id),
    onRefinePlan: (t: Todo[]) => cbs.current.onRefinePlan(t),
  }), []);

  return (
    <div className="sx-feedwrap">
      {/* The conversation updates on its own as agents work, so a screen reader
          needs to be told. "polite" waits for a pause rather than interrupting
          every streamed token. */}
      <div className="sx-feed" ref={scroller} onScroll={onScroll} onWheel={onWheel}
        onPointerDown={(e) => { if (e.target === scroller.current) dragging.current = true; }}
        onTouchStart={(e) => { touchY.current = e.touches[0]?.clientY ?? null; }}
        onTouchMove={(e) => {
          const y = e.touches[0]?.clientY;
          if (touchY.current !== null && y !== undefined && y - touchY.current > 4 && !innerTakesIt(e.target)) letGo();
        }}
        onTouchEnd={() => { touchY.current = null; }} onTouchCancel={() => { touchY.current = null; }}
        role="log" aria-live="polite" aria-relevant="additions" aria-label="Research conversation">
        <div className="sx-feed-inner" ref={inner}>
          {st.items.length === 0 && !st.live && <Welcome onExample={onExample} />}
          {st.items.map((it) => {
            const live = st.running && it.id === lastStepsId;
            const sp = it.k === "spec" ? st.specialists[it.specId] : undefined;
            const art = it.k === "art" ? st.artifacts[it.artId]
              : it.k === "user" && it.artifactId ? st.artifacts[it.artifactId] : undefined;
            // The clock only matters to what is still running.
            const ticks = live || (sp && sp.status === "working");
            return (
              <FeedItem key={it.id} it={it} sp={sp} art={art} now={ticks ? now : 0}
                liveSteps={live} workingDir={workingDir} {...stable} />
            );
          })}
          {st.live && st.live.text && (
            <div className="sx-msg assistant streaming">
              <Who who={st.live.who} icon={st.live.icon} />
              <Markdown text={st.live.text} />
              <span className="sx-caret" />
            </div>
          )}
        </div>
      </div>
      {/* The way back to the work. It floats over the conversation rather than
          sitting in it, so showing or hiding it never moves the content. */}
      {away && (
        <button className={"sx-jump" + (st.running ? " live" : "")} onClick={jump}
          title="Scroll to the latest and keep following it">
          {st.running && <span className="sx-pulse sm" aria-hidden />}
          <span className="sx-jump-t">{st.running ? "Jump to current work" : "Jump to latest"}</span>
          {fresh > 0 && <span className="sx-jump-n">{fresh} new</span>}
          <span className="sx-jump-arrow" aria-hidden>↓</span>
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

const FeedItem = memo(function FeedItem({ it, sp, art, now, onOpenArt, onRefine, onHide, onRefinePlan, liveSteps, workingDir }: {
  it: Item; sp?: Specialist; art?: Artifact; now: number; onOpenArt: (id: string) => void;
  onRefine: (id: string) => void; onHide: (id: string) => void; onRefinePlan: (todos: Todo[]) => void;
  liveSteps: boolean; workingDir: string;
}) {
  switch (it.k) {
    case "compacted":
      return <CompactedCard d={it.digest} />;
    case "user":
      return <UserMsg it={it} art={art} />;
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
    case "spec":
      return sp ? <SpecCard sp={sp} runIdx={it.run} now={now || Date.now()} workingDir={workingDir} /> : null;
    case "art":
      return art ? <ArtCard a={art} onOpen={() => onOpenArt(art.id)} onRefine={() => onRefine(art.id)}
        onHide={() => onHide(art.id)} /> : null;
    case "note":
      return (
        <div className={"sx-note " + (it.tone || "")}>
          {it.who && <span className="sx-note-who">{it.icon} {it.who}</span>}
          <span className="sx-note-text">{it.text}</span>
        </div>
      );
  }
  return null;
});

function fmtDay(ts?: number): string {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "";
}

/** The earlier part of a long session, folded away between turns. Its detail is
 *  archived on disk; what lasts is in the outputs, the provenance and the files. */
function CompactedCard({ d }: { d: Digest }) {
  const [open, setOpen] = useState(false);
  const turns = d.turns || [];
  const total = turns.length + (d.earlier_turns || 0);
  const outs = Object.keys(d.outputs || {});
  const jobs = Object.entries(d.jobs || {});
  const span = d.from ? `${fmtDay(d.from)}${d.to && fmtDay(d.to) !== fmtDay(d.from) ? " – " + fmtDay(d.to) : ""}` : "";
  return (
    <div className="sx-compacted">
      <button className="sx-compacted-head" onClick={() => setOpen((o) => !o)}>
        <Chev open={open} />
        <span className="sx-compacted-title">Earlier in this session{span ? ` · ${span}` : ""}</span>
        <span className="sx-compacted-meta">
          {total} turn{total === 1 ? "" : "s"} · {outs.length} output{outs.length === 1 ? "" : "s"} · {jobs.length} job{jobs.length === 1 ? "" : "s"} — summarised
        </span>
      </button>
      {open && (
        <div className="sx-compacted-body">
          <p className="sx-compacted-why">
            The step-by-step of this part is archived to keep the session fast. Its
            results are in the Outputs and Provenance tabs and in the project files.
          </p>
          {d.earlier_turns > 0 && <div className="sx-compacted-more">… {d.earlier_turns} earlier turns</div>}
          {turns.map((t, i) => (
            <div key={i} className="sx-compacted-turn">
              <div className="sx-compacted-q">{t.auto ? "⏰ " : ""}{t.asked}{t.ts ? <span className="sx-compacted-day"> · {fmtDay(t.ts)}</span> : null}</div>
              {t.answer && <div className="sx-compacted-a">{t.answer}</div>}
            </div>
          ))}
          {jobs.length > 0 && (
            <div className="sx-compacted-list">
              <b>Jobs:</b> {jobs.map(([n, st]) => `${n} (${st})`).join(", ")}
            </div>
          )}
          {Object.keys(d.specialists || {}).length > 0 && (
            <div className="sx-compacted-list">
              <b>Specialists:</b> {Object.entries(d.specialists).map(([n, r]) => r ? `${n} — ${r}` : n).join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Mount ``children`` only once scrolled near view, then keep them: a long
 *  conversation's output cards would otherwise all load their iframes and
 *  tables at once when the session opens. */
function WhenVisible({ children, minHeight = 120 }: { children: ReactNode; minHeight?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(typeof IntersectionObserver === "undefined");
  useEffect(() => {
    if (seen || !ref.current) return;
    const io = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting)) { setSeen(true); io.disconnect(); }
    }, { rootMargin: "600px 0px" });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [seen]);
  return <div ref={ref} style={seen ? undefined : { minHeight }}>{seen ? children : null}</div>;
}

const STEER_LABEL: Record<string, string> = {
  sending: "sending…",
  queued: "queued — the orchestrator picks this up at its next step",
  delivered: "picked up",
  dropped: "not delivered — the turn was stopped first",
  failed: "not sent — see the note below, then send it again",
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

function SpecCard({ sp, runIdx, now, workingDir }:
                  { sp: Specialist; runIdx: number; now: number; workingDir: string }) {
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
        <span className={"sx-pill " + run.status}
              title={run.status === "incomplete"
                ? "This specialist did not finish — it can be resumed." : undefined}>
          {working ? <><span className="sx-spin sm" /> working</> : run.status}
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
      {/* What it said as it worked. Watching it live and then losing it the
          moment it finishes is how the reasoning behind a result disappears —
          the summary is the conclusion, this is how it got there. */}
      {!working && run.said.length > 0 && (
        <details className="sx-spec-sum">
          <summary>Working notes · {run.said.length} message{run.said.length === 1 ? "" : "s"}</summary>
          {run.said.map((t, i) => <Markdown key={i} text={t} />)}
        </details>
      )}
      {/* Files it offered the orchestrator. Ones that were not presented exist
          only on disk, so name them here rather than losing them. */}
      {!working && (run.proposed?.length || 0) > 0 && (
        <details className="sx-spec-sum">
          <summary>Proposed {run.proposed!.length} output{run.proposed!.length === 1 ? "" : "s"} · the orchestrator decides which are shown</summary>
          <ul className="sx-spec-prop">
            {run.proposed!.map((p, i) => (
              <li key={i}><code>{p.rel}</code>{p.interim ? <span className="sx-tag">interim</span> : null}
                {p.caption ? <span className="sx-spec-propcap"> — {p.caption}</span> : null}</li>
            ))}
          </ul>
        </details>
      )}
      {run.steps.length > 0 && (
        <>
          <button className="sx-spec-toggle" onClick={() => setOpen((o) => !o)}>
            <Chev open={open} /> {run.steps.length} step{run.steps.length === 1 ? "" : "s"}
            {run.steps.some((s) => s.ok === false) ? " · some failed" : ""}
          </button>
          {open && <div className="sx-steps-list">{run.steps.map((s, i) => <StepRow key={i} s={s} now={now} />)}</div>}
        </>
      )}
      {!working && workingDir && <Transcript specId={sp.id} workingDir={workingDir} />}
    </div>
  );
}

/** The specialist's own transcript, fetched on demand.
 *
 *  Its report is a claim; this is the work behind it. It is written to disk on
 *  every run and was unreachable from the page, so a relayed summary had to be
 *  taken on trust. Loaded only when asked for — a transcript can be large. */
function Transcript({ specId, workingDir }: { specId: string; workingDir: string }) {
  const [state, setState] = useState<"idle" | "loading" | "error" | "ready">("idle");
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState("");

  const load = async () => {
    if (state === "loading") return;
    if (state === "ready") { setState("idle"); return; }
    setState("loading");
    try {
      const r = await fetch(`/api/science/transcript?working_dir=${encodeURIComponent(workingDir)}`
        + `&id=${encodeURIComponent(specId)}`);
      const j = await r.json();
      if (!j.ok) { setErr(j.error || "could not read the transcript"); setState("error"); return; }
      setData(j); setState("ready");
    } catch (e: any) {
      setErr(String(e?.message || e)); setState("error");
    }
  };

  return (
    <>
      <button className="sx-spec-toggle" onClick={load} aria-expanded={state === "ready"}>
        <Chev open={state === "ready"} /> {state === "loading" ? "Loading transcript…" : "Transcript"}
      </button>
      {state === "error" && <div className="sx-spec-now muted">⚠ {err}</div>}
      {state === "ready" && data && (
        <div className="sx-tr">
          {data.truncated && (
            <div className="sx-tr-note">Showing the last {data.steps.length} of {data.total} entries.</div>
          )}
          {data.steps.map((x: any, i: number) => (
            <div key={i} className={"sx-tr-row " + x.kind}>
              <span className="sx-tr-kind">
                {x.kind === "call" ? toolIcon(x.name) : x.kind === "result" ? "↳"
                  : x.kind === "said" ? "💬" : x.kind === "note" ? (x.from === "researcher" ? "👤" : "ℹ️") : "🎯"}
              </span>
              <div className="sx-tr-body">
                {x.kind === "call" && <><b>{x.name}</b><pre>{x.args}</pre></>}
                {x.kind === "result" && <pre>{x.text}</pre>}
                {x.kind === "said" && <Markdown text={x.text} />}
                {x.kind === "task" && <><b>brief</b><pre>{x.text}</pre></>}
                {x.kind === "note" && <><b>{x.from === "researcher" ? "message from the researcher" : "note from the runtime"}</b><pre>{x.text}</pre></>}
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function tail(t: string, n: number): string {
  return t.length > n ? "…" + t.slice(-n) : t;
}

// ---------------------------------------------------------------------------

function ArtCard({ a, onOpen, onRefine, onHide }: {
  a: Artifact; onOpen: () => void; onRefine: () => void; onHide: () => void;
}) {
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
        <WhenVisible><FileView url={artUrl(a)} rel={a.rel} kind={a.kind} size="compact" /></WhenVisible>
      </div>
      <div className="sx-art-actions">
        <button className="sx-btn ghost sm" onClick={onOpen}>Open</button>
        <button className="sx-btn ghost sm" onClick={onRefine}>Refine…</button>
        <a className="sx-btn ghost sm" href={artUrl(a)} target="_blank" rel="noreferrer">↗</a>
        <button className="sx-btn ghost sm" onClick={onHide}
          title="Not relevant? Take it off your outputs — the file stays on disk">Hide</button>
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
