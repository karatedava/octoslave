// State of one Science session, driven entirely by server events.
//
// Live events and the persisted timeline the server replays on open go through
// the SAME reducer, so a reloaded session looks exactly like it did live:
// steps, specialists and outputs in place, not just chat bubbles.
import type { Ask } from "../AskCard";
import { askFromEvent } from "../AskCard";

export type Speaker = { agentId?: string; who?: string; icon?: string };

export type Step = {
  name: string;
  summary: string;
  ok?: boolean;          // undefined while running
  preview?: string;      // result preview
  args?: any;            // args_preview (bash command, file content, …)
  at: number;
  end?: number;
};

export type SteerState = "sending" | "queued" | "seen" | "delivered" | "dropped";

export type Item =
  | { k: "user"; id: number; text: string; cid?: string; steerId?: string;
      steer?: SteerState; seenBy?: string; artifactId?: string; at: number }
  | { k: "assistant"; id: number; text: string; tone?: string } & Speaker
  | { k: "steps"; id: number; steps: Step[] }
  | { k: "spec"; id: number; specId: string; run: number }
  | { k: "art"; id: number; artId: string; by?: string; icon?: string }
  | { k: "plan"; id: number; todos: Todo[]; revised: boolean }
  | { k: "note"; id: number; text: string; tone?: string } & Speaker;

export type Artifact = {
  id: string; rel: string; path: string; caption: string; kind: string;
  provenance?: string; interim?: boolean; by?: string;
  comments?: { text: string; at: string }[];
  ver: number;           // bumped on every update → cache-busts previews
  at: number;
};

export type SpecRun = {
  steps: Step[];
  live: string;          // text being streamed right now
  said: string[];        // finished messages of this run
  status: "working" | "done" | "failed";
  summary?: string;
  started: number;
  ended?: number;
  resumed?: boolean;
  thinking: number;
};

export type Specialist = {
  id: string; name: string; role: string; goal: string; icon: string;
  status: string; summary: string; tools: string[]; model?: string;
  runs: SpecRun[];
};

export type Job = {
  id: string; name: string; status: string; remote_label: string;
  scheduler: string; handle: string; output?: string; at: number;
};

export type Prov = { artifact: string; method?: string; inputs?: string; notes?: string; at?: string };
export type Todo = { content: string; status: string };

export type Live = { text: string } & Speaker;

export type Activity = { label: string; since: number; name?: string } & Speaker;

export type SciState = {
  items: Item[];
  nextId: number;
  artifacts: Record<string, Artifact>;
  artOrder: string[];
  specialists: Record<string, Specialist>;
  specOrder: string[];
  jobs: Record<string, Job>;
  jobOrder: string[];
  prov: Prov[];
  todos: Todo[];
  running: boolean;
  live: Live | null;           // the ORCHESTRATOR's stream (specialists stream into their run)
  thinking: number;            // reasoning chars since the last visible output
  activity: Activity | null;   // what is happening right now
  ask: Ask | null;
  lastAssistant: string;
  lastArt: { id: string; isNew: boolean; n: number } | null;  // for auto-follow in the workspace
  turnStarted: number;
};

export const initialState: SciState = {
  items: [], nextId: 1, artifacts: {}, artOrder: [], specialists: {}, specOrder: [],
  jobs: {}, jobOrder: [], prov: [], todos: [], running: false, live: null, thinking: 0,
  activity: null, ask: null, lastAssistant: "", lastArt: null, turnStarted: 0,
};

export type Action =
  | { type: "reset" }
  | { type: "hydrate"; snapshot: any; events: any[]; running: boolean }
  | { type: "event"; ev: any; replay?: boolean }
  | { type: "local_user"; cid: string; text: string; artifactId?: string; steering: boolean }
  | { type: "clear_ask" }
  | { type: "answered"; text: string };

const CARDED_TOOLS = new Set(["spawn_specialist", "continue_specialist", "present_output"]);

function speakerOf(ev: any): Speaker {
  return ev && ev.agent_name
    ? { agentId: ev.agent_id, who: ev.agent_name, icon: ev.agent_icon || "🔬" }
    : {};
}

// ---------------------------------------------------------------------------

export function reducer(s: SciState, a: Action): SciState {
  switch (a.type) {
    case "reset":
      return initialState;
    case "hydrate": {
      let st: SciState = { ...initialState };
      const snap = a.snapshot || {};
      // Seed from the snapshot (authoritative for current status); the timeline
      // replay below then places everything in the conversation.
      for (const sp of snap.specialists || []) {
        st.specialists = { ...st.specialists, [sp.id]: {
          id: sp.id, name: sp.name, role: sp.role, goal: sp.goal, icon: sp.icon || "🔬",
          status: sp.status, summary: sp.summary || "", tools: sp.tools || [], runs: [] } };
        st.specOrder = [...st.specOrder, sp.id];
      }
      for (const j of snap.jobs || []) {
        st.jobs = { ...st.jobs, [j.id]: { ...j, at: 0 } };
        st.jobOrder = [...st.jobOrder, j.id];
      }
      st.prov = snap.provenance || [];
      for (const ev of a.events) st = onEvent(st, ev, true);
      st = { ...st, specialists: { ...st.specialists }, artifacts: { ...st.artifacts } };
      // Snapshot status wins over whatever the replay reconstructed.
      for (const sp of snap.specialists || []) {
        const cur = st.specialists[sp.id];
        if (cur) st.specialists[sp.id] = { ...cur, status: sp.status, summary: sp.summary || cur.summary };
      }
      for (const art of snap.artifacts || []) {
        const cur = st.artifacts[art.id];
        st.artifacts[art.id] = { ...(cur || { ver: 1, at: 0 }), ...art, ver: cur?.ver || 1 } as Artifact;
        if (!cur) {
          st.artOrder = [...st.artOrder, art.id];
          st = push(st, { k: "art", artId: art.id });
        }
      }
      st.prov = snap.provenance || st.prov;
      st.running = a.running;
      if (!a.running) {
        st.live = null;
        // Mark any orphaned "running" step/run as finished — the turn is over.
        st = settleAll(st);
      } else {
        st.turnStarted = Date.now();
      }
      st.lastArt = null;
      return st;
    }
    case "event":
      return onEvent(s, a.ev, !!a.replay);
    case "local_user":
      return {
        ...push(s, { k: "user", text: a.text, cid: a.cid, artifactId: a.artifactId,
                     steer: a.steering ? "sending" : undefined, at: Date.now() }),
        running: true,
        turnStarted: s.running ? s.turnStarted : Date.now(),
      };
    case "clear_ask":
      return { ...s, ask: null };
    case "answered":
      return { ...push(s, { k: "user", text: a.text, at: Date.now() }), ask: null };
  }
}

function push(s: SciState, item: any): SciState {
  return { ...s, items: [...s.items, { ...item, id: s.nextId }], nextId: s.nextId + 1 };
}

function note(s: SciState, text: string, tone?: string, sp: Speaker = {}): SciState {
  return push(s, { k: "note", text, tone, ...sp });
}

function mapItems(s: SciState, f: (it: Item) => Item): SciState {
  return { ...s, items: s.items.map(f) };
}

function commitLive(s: SciState, text?: string, sp?: Speaker): SciState {
  const t = (text ?? s.live?.text ?? "").trim();
  const who = sp ?? (s.live ? { agentId: s.live.agentId, who: s.live.who, icon: s.live.icon } : {});
  let st: SciState = { ...s, live: null };
  if (t && t !== s.lastAssistant) {
    st = push(st, { k: "assistant", text: t, ...who });
    st.lastAssistant = t;
  }
  return st;
}

function updateSpec(s: SciState, id: string, f: (sp: Specialist) => Specialist): SciState {
  const cur = s.specialists[id];
  if (!cur) return s;
  return { ...s, specialists: { ...s.specialists, [id]: f(cur) } };
}

function updateRun(s: SciState, id: string, f: (r: SpecRun) => SpecRun): SciState {
  return updateSpec(s, id, (sp) => {
    if (!sp.runs.length) return sp;
    const runs = sp.runs.slice();
    runs[runs.length - 1] = f(runs[runs.length - 1]);
    return { ...sp, runs };
  });
}

// A specialist event whose specialist we have never seen (e.g. the log was
// trimmed) still needs somewhere to go.
function ensureSpec(s: SciState, ev: any): SciState {
  const id = ev.agent_id;
  if (!id || s.specialists[id]) return s;
  const sp: Specialist = {
    id, name: ev.agent_name || "Specialist", role: ev.agent_role || "", goal: "",
    icon: ev.agent_icon || "🔬", status: "working", summary: "", tools: [],
    runs: [{ steps: [], live: "", said: [], status: "working", started: Date.now(), thinking: 0 }],
  };
  let st = { ...s, specialists: { ...s.specialists, [id]: sp }, specOrder: [...s.specOrder, id] };
  st = push(st, { k: "spec", specId: id, run: 0 });
  return st;
}

function settleSteps(steps: Step[]): Step[] {
  return steps.map((x) => (x.ok === undefined ? { ...x, ok: true, end: x.end || x.at } : x));
}

function settleAll(s: SciState): SciState {
  let st = mapItems(s, (it) => (it.k === "steps" ? { ...it, steps: settleSteps(it.steps) } : it));
  const specs: Record<string, Specialist> = {};
  for (const [id, sp] of Object.entries(st.specialists)) {
    specs[id] = { ...sp, runs: sp.runs.map((r) =>
      r.status === "working" ? { ...r, status: "done", live: "", steps: settleSteps(r.steps) }
                             : { ...r, live: "" }) };
    if (sp.status === "working") specs[id].status = "done";
  }
  st = { ...st, specialists: specs, activity: null, thinking: 0 };
  return st;
}

function onEvent(s: SciState, ev: any, replay: boolean): SciState {
  const sp = speakerOf(ev);
  const now = Date.now();
  switch (ev.type) {
    case "science_user": {
      // Our own optimistic message coming back: attach the server's ids.
      if (ev.cid) {
        const idx = s.items.findIndex((it) => it.k === "user" && it.cid === ev.cid);
        if (idx >= 0) {
          return mapItems({ ...s, running: true }, (it) => it.k === "user" && it.cid === ev.cid
            ? { ...it, steerId: ev.steer_id, steer: ev.queued ? "queued" : undefined } : it);
        }
      }
      const st = push(s, { k: "user", text: ev.text || "", steerId: ev.steer_id,
        steer: ev.queued ? "queued" : undefined, artifactId: ev.artifact_id, at: now });
      return replay ? st : { ...st, running: true, turnStarted: s.running ? s.turnStarted : now };
    }
    case "science_steer": {
      const ids: string[] = ev.ids || [];
      const state: SteerState = ev.dropped ? "dropped"
        : ev.to === "specialist" ? "seen" : "delivered";
      return mapItems(s, (it) => {
        if (it.k !== "user" || !it.steerId || !ids.includes(it.steerId)) return it;
        // "seen" by a specialist must not demote an already-delivered message.
        if (state === "seen" && it.steer === "delivered") return it;
        return { ...it, steer: state, seenBy: state === "seen" ? ev.to_name : it.seenBy };
      });
    }
    case "stream_start":
      if (sp.agentId) return updateRun(ensureSpec(s, ev), sp.agentId, (r) => ({ ...r, live: "", thinking: 0 }));
      return { ...s, live: { text: "", ...sp }, thinking: 0 };
    case "token": {
      if (sp.agentId) {
        return updateRun(ensureSpec(s, ev), sp.agentId,
          (r) => ({ ...r, live: r.live + (ev.text || ""), thinking: 0 }));
      }
      const live = s.live || { text: "", ...sp };
      return { ...s, live: { ...live, text: live.text + (ev.text || "") }, thinking: 0,
               activity: null };
    }
    case "reasoning":
      if (sp.agentId) return updateRun(s, sp.agentId, (r) => ({ ...r, thinking: r.thinking + (ev.text || "").length }));
      return { ...s, thinking: s.thinking + (ev.text || "").length };
    case "stream_end": {
      if (sp.agentId) {
        return updateRun(s, sp.agentId, (r) => {
          const t = r.live.trim();
          return { ...r, live: "", said: t && !ev.aborted ? [...r.said, t] : r.said };
        });
      }
      if (ev.aborted) {
        const had = !!s.live?.text.trim();
        const st = { ...s, live: null };
        return had ? note(st, "↺ That response was cut off mid-stream and discarded — the model will redo this step.", "bad") : st;
      }
      return commitLive(s);
    }
    case "assistant_message": {
      if (sp.agentId) {
        const st = ensureSpec(s, ev);
        return updateRun(st, sp.agentId, (r) => ({ ...r, said: [...r.said, ev.text || ""] }));
      }
      return commitLive({ ...s, live: null }, ev.text || "", sp);
    }
    case "science_reply": {
      let st = s.live ? commitLive(s) : s;
      const t = (ev.text || "").trim();
      if (t && t !== st.lastAssistant) {
        st = push(st, { k: "assistant", text: t, tone: ev.stopped ? "warn" : undefined });
        st.lastAssistant = t;
      }
      return st;
    }
    case "science_done": {
      let st = s.live ? commitLive(s) : s;
      st = settleAll(st);
      return { ...st, running: false, ask: null, live: null };
    }
    case "tool_call": {
      const step: Step = { name: ev.name, summary: ev.summary || ev.name, args: ev.args_preview, at: now };
      // ``activity`` is the ORCHESTRATOR's current step. Specialists run in
      // parallel with it; their current step lives in their own run, so one of
      // theirs must never overwrite (or later clear) the orchestrator's.
      if (sp.agentId) {
        const st = ensureSpec(s, ev);
        return updateRun(st, sp.agentId, (r) => ({ ...r, steps: [...r.steps, step], thinking: 0 }));
      }
      const activity: Activity | null = replay ? s.activity : { label: step.summary, since: now, name: ev.name };
      // A specialist or an output has its own card in the conversation; a step
      // row for the call that created it would only repeat it.
      if (CARDED_TOOLS.has(ev.name)) return { ...s, activity, thinking: 0 };
      const last = s.items[s.items.length - 1];
      if (last && last.k === "steps") {
        return { ...mapItems(s, (it) => it.id === last.id && it.k === "steps"
          ? { ...it, steps: [...it.steps, step] } : it), activity, thinking: 0 };
      }
      return { ...push(s, { k: "steps", steps: [step] }), activity, thinking: 0 };
    }
    case "tool_result": {
      const close = (steps: Step[]) => {
        const out = steps.slice();
        for (let i = out.length - 1; i >= 0; i--) {
          if (out[i].ok === undefined && out[i].name === ev.name) {
            out[i] = { ...out[i], ok: ev.ok !== false, preview: ev.preview, end: now };
            break;
          }
        }
        return out;
      };
      if (sp.agentId) return updateRun(s, sp.agentId, (r) => ({ ...r, steps: close(r.steps) }));
      const st = { ...s, activity: replay ? s.activity : null };
      for (let i = st.items.length - 1; i >= 0; i--) {
        const it = st.items[i];
        if (it.k === "steps") {
          return mapItems(st, (x) => (x.id === it.id && x.k === "steps" ? { ...x, steps: close(x.steps) } : x));
        }
      }
      return st;
    }
    case "todos": {
      const todos: Todo[] = (ev.todos || []).map((t: any) =>
        ({ content: t.content || t.text || "", status: t.status || "pending" }));
      // The plan itself — as opposed to progress through it — is something the
      // researcher should see and be able to change, so a new or reworked plan
      // gets its own card in the conversation. Ticking a step off does not.
      const before = s.todos.map((t) => t.content).join("\u0000");
      const after = todos.map((t) => t.content).join("\u0000");
      if (after && after !== before) {
        let st = { ...s, todos };
        if (st.live) st = commitLive(st);
        return push(st, { k: "plan", todos, revised: s.todos.length > 0 });
      }
      return { ...s, todos };
    }
    case "plan":
      return ev.text ? note(s, "🗺 Plan:\n" + ev.text, "info") : s;
    case "science_specialist": {
      if (ev.event === "start") {
        const prev = s.specialists[ev.id];
        const run: SpecRun = { steps: [], live: "", said: [], status: "working", started: now,
                               resumed: !!ev.resumed, thinking: 0 };
        const spc: Specialist = {
          id: ev.id, name: ev.name, role: ev.role, goal: ev.goal, icon: ev.icon || "🔬",
          status: "working", summary: prev?.summary || "", tools: ev.tools || prev?.tools || [],
          model: ev.model || prev?.model, runs: [...(prev?.runs || []), run],
        };
        let st: SciState = { ...s, specialists: { ...s.specialists, [ev.id]: spc },
          specOrder: prev ? s.specOrder : [...s.specOrder, ev.id] };
        // The orchestrator's reply so far belongs before the specialist's card.
        if (st.live) st = commitLive(st);
        return push(st, { k: "spec", specId: ev.id, run: spc.runs.length - 1 });
      }
      if (ev.event === "done") {
        const status = ev.status === "failed" ? "failed" : "done";
        let st = updateRun(s, ev.id, (r) => ({ ...r, status, summary: ev.summary || "", ended: now,
                                               live: "", steps: settleSteps(r.steps) }));
        return updateSpec(st, ev.id, (x) => ({ ...x, status, summary: ev.summary || x.summary }));
      }
      return s;
    }
    case "science_job": {
      const prev = s.jobs[ev.id];
      const job: Job = { id: ev.id, name: ev.name, status: ev.status, remote_label: ev.remote,
        scheduler: ev.scheduler, handle: ev.handle, output: ev.output, at: prev?.at || now };
      let st: SciState = { ...s, jobs: { ...s.jobs, [ev.id]: job },
        jobOrder: prev ? s.jobOrder : [...s.jobOrder, ev.id] };
      if (!prev || prev.status !== ev.status) {
        const icon = ev.status === "failed" ? "⚠" : ev.status === "done" ? "✓" : "🖥";
        st = note(st, `${icon} Job “${ev.name}” on ${ev.remote || "local"} — ${ev.status}`,
          ev.status === "failed" ? "bad" : ev.status === "done" ? "ok" : "job", sp);
      }
      return st;
    }
    case "science_artifact": {
      const prev = s.artifacts[ev.id];
      const art: Artifact = {
        ...(prev || {}), id: ev.id, rel: ev.rel, path: ev.path, caption: ev.caption, kind: ev.kind,
        provenance: ev.provenance, interim: !!ev.interim, by: sp.who || prev?.by,
        ver: (prev?.ver || 0) + 1, at: now, comments: prev?.comments,
      };
      let st: SciState = { ...s, artifacts: { ...s.artifacts, [ev.id]: art },
        artOrder: prev ? s.artOrder : [...s.artOrder, ev.id] };
      if (st.live && !sp.agentId) st = commitLive(st);
      const hasCard = st.items.some((it) => it.k === "art" && it.artId === ev.id);
      if (!hasCard) st = push(st, { k: "art", artId: ev.id, by: sp.who, icon: sp.icon });
      else if (!replay) st = note(st, `🔄 Updated — ${ev.caption || ev.rel}`, "ok", sp);
      if (!replay) st.lastArt = { id: ev.id, isNew: !prev, n: (s.lastArt?.n || 0) + 1 };
      return st;
    }
    case "science_provenance":
      return { ...s, prov: [...s.prov, { ...(ev.entry || {}) }] };
    case "user_question":
      return replay ? s : { ...s, ask: { ...askFromEvent(ev), who: ev.agent_name || undefined } };
    case "user_question_closed":
      if (replay) return s;
      return s.ask && !ev.answered
        ? note({ ...s, ask: null }, "⏳ That question timed out — carrying on with best judgement.", "warn")
        : { ...s, ask: null };
    case "agent_event":
      if (ev.event === "model_switch" && !replay) {
        return note(s, `↔ ${ev.agent || "Specialist"}: ${ev.from_model} → ${ev.to_model} (${ev.reason || "switch"})`, "info");
      }
      return s;
    case "info":
      return ev.text ? note(s, ev.text, "info", sp) : s;
    case "error":
      return ev.text ? note(s, ev.text, "bad", sp) : s;
  }
  return s;
}
