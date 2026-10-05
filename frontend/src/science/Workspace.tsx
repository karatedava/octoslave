import { useEffect, useState } from "react";
import { Markdown } from "./Markdown";
import { FileView } from "./Viewers";
import { useNow } from "./Feed";
import type { Artifact, SciState } from "./store";
import { artUrl, baseName, EDITABLE, fmtDur, fmtWhen, kindIcon, mediaType, viewUrl } from "./util";

export type Doc = { type: "art"; id: string } | { type: "file"; path: string; rel: string } | null;
export type WsTab = "preview" | "outputs" | "team" | "jobs" | "prov";

type Props = {
  st: SciState;
  workingDir: string;
  tab: WsTab;
  setTab: (t: WsTab) => void;
  doc: Doc;
  setDoc: (d: Doc) => void;
  follow: boolean;
  setFollow: (f: boolean) => void;
  onRefine: (artId: string) => void;
  onFocusSpec: (id: string) => void;
  onMention: (rel: string) => void;
};

export function Workspace(p: Props) {
  const { st } = p;
  const working = Object.values(st.specialists).filter((s) => s.status === "working").length;
  const activeJobs = Object.values(st.jobs).filter((j) => ["submitted", "running"].includes(j.status)).length;
  const tabs: { id: WsTab; label: string; badge?: string | number; live?: boolean }[] = [
    { id: "preview", label: "Preview" },
    { id: "outputs", label: "Outputs", badge: st.artOrder.length || undefined },
    { id: "team", label: "Team", badge: st.specOrder.length || undefined, live: working > 0 },
    { id: "jobs", label: "Jobs", badge: st.jobOrder.length || undefined, live: activeJobs > 0 },
    { id: "prov", label: "Provenance", badge: st.prov.length || undefined },
  ];
  return (
    <section className="sx-ws">
      <div className="sx-tabs">
        {tabs.map((t) => (
          <button key={t.id} className={"sx-tab" + (p.tab === t.id ? " on" : "")} onClick={() => p.setTab(t.id)}>
            {t.label}
            {t.live && <span className="sx-livedot" />}
            {t.badge !== undefined && <span className="sx-badge">{t.badge}</span>}
          </button>
        ))}
      </div>
      <div className="sx-ws-body">
        {p.tab === "preview" && <Preview {...p} />}
        {p.tab === "outputs" && (
          <Outputs st={st} onPick={(id) => { p.setDoc({ type: "art", id }); p.setTab("preview"); }} />
        )}
        {p.tab === "team" && <Team st={st} onFocus={p.onFocusSpec} />}
        {p.tab === "jobs" && <Jobs st={st} />}
        {p.tab === "prov" && <Provenance st={st} workingDir={p.workingDir} />}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------

function Preview({ st, doc, setDoc, follow, setFollow, onRefine, onMention, workingDir }: Props) {
  const art: Artifact | undefined = doc?.type === "art" ? st.artifacts[doc.id] : undefined;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState("");
  const [bust, setBust] = useState(0);
  useEffect(() => { setEditing(false); setMsg(""); }, [doc && (doc.type === "art" ? doc.id : doc.path)]);

  if (!doc || (doc.type === "art" && !art)) {
    return (
      <div className="sx-empty">
        <div className="sx-empty-ic">◰</div>
        <div className="sx-empty-h">Live results appear here</div>
        <div>
          Plots, tables and reports open in this panel the moment the orchestrator or a
          specialist presents them — including early previews while work continues.
          Pick any file in the explorer to look at it too.
        </div>
        <label className="sx-follow">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          Follow new results
        </label>
      </div>
    );
  }

  const path = art ? art.path : (doc as any).path;
  const rel = art ? art.rel : (doc as any).rel;
  const url = art ? artUrl(art, bust) : `${viewUrl(path)}?r=${bust}`;
  const editable = EDITABLE.test(rel) && mediaType(rel) !== "image";
  const idx = art ? st.artOrder.indexOf(art.id) : -1;
  const go = (d: number) => {
    const id = st.artOrder[idx + d];
    if (id) { setDoc({ type: "art", id }); setFollow(false); }
  };

  async function startEdit() {
    try {
      const r = await fetch(url);
      setDraft(await r.text());
      setEditing(true);
    } catch { setMsg("couldn't load the file"); }
  }
  async function save() {
    try {
      const r = await fetch("/api/science/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, content: draft }),
      });
      const d = await r.json();
      if (d.ok) { setEditing(false); setBust((b) => b + 1); setMsg("saved"); }
      else setMsg(d.error || "save failed");
    } catch { setMsg("save failed"); }
  }

  return (
    <div className="sx-preview">
      <div className="sx-pv-head">
        <span className="sx-pv-ic">{art ? kindIcon(art.kind, art.rel) : kindIcon("", rel)}</span>
        <div className="sx-pv-title">
          <div className="sx-pv-name" title={path}>{art ? (art.caption || art.rel) : baseName(rel)}</div>
          <div className="sx-pv-meta">
            {rel}
            {art?.by && <> · by {art.by}</>}
            {art && art.ver > 1 && <> · v{art.ver}</>}
            {art?.interim && <span className="sx-tag warn">preview</span>}
          </div>
        </div>
        {art && st.artOrder.length > 1 && (
          <div className="sx-pv-nav">
            <button className="sx-icon-btn" disabled={idx <= 0} onClick={() => go(-1)} title="Previous output">‹</button>
            <span>{idx + 1}/{st.artOrder.length}</span>
            <button className="sx-icon-btn" disabled={idx >= st.artOrder.length - 1} onClick={() => go(1)} title="Next output">›</button>
          </div>
        )}
      </div>
      <div className="sx-pv-tools">
        {art && <button className="sx-btn sm primary" onClick={() => onRefine(art.id)}>Refine…</button>}
        {!art && <button className="sx-btn sm" onClick={() => onMention(rel)} title="Reference this file in your next message">Ask about this</button>}
        {editable && !editing && <button className="sx-btn sm" onClick={startEdit}>Edit</button>}
        <a className="sx-btn sm" href={url} target="_blank" rel="noreferrer">Open ↗</a>
        <span className="sx-spacer" />
        {msg && <span className="sx-muted">{msg}</span>}
        <label className="sx-follow" title="Jump to each new result as it is presented">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          Follow new results
        </label>
      </div>
      <div className="sx-pv-body">
        {editing ? (
          <div className="sx-editor">
            <textarea value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} />
            <div className="sx-editor-bar">
              <button className="sx-btn sm primary" onClick={save}>Save</button>
              <button className="sx-btn sm" onClick={() => setEditing(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <FileView key={url} url={url} rel={rel} kind={art?.kind} size="full" />
        )}
      </div>
      {art && (art.provenance || (art.comments && art.comments.length > 0)) && (
        <div className="sx-pv-foot">
          {art.provenance && <div className="sx-pv-prov">📎 {art.provenance}</div>}
          {(art.comments || []).map((c, i) => (
            <div key={i} className="sx-pv-comment"><span>{fmtWhen(c.at)}</span>{c.text}</div>
          ))}
        </div>
      )}
      <div className="sx-pv-wd" title={workingDir}>{path}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function Outputs({ st, onPick }: { st: SciState; onPick: (id: string) => void }) {
  const [filter, setFilter] = useState<"all" | "image" | "table" | "doc">("all");
  const arts = st.artOrder.slice().reverse().map((id) => st.artifacts[id]).filter(Boolean).filter((a) => {
    const m = mediaType(a.rel, a.kind);
    return filter === "all" || (filter === "image" && m === "image") || (filter === "table" && m === "table")
      || (filter === "doc" && m !== "image" && m !== "table");
  });
  if (!st.artOrder.length) {
    return <div className="sx-empty"><div className="sx-empty-ic">▦</div>No outputs yet. Everything the agents present collects here.</div>;
  }
  return (
    <div className="sx-outputs">
      <div className="sx-chips">
        {(["all", "image", "table", "doc"] as const).map((f) => (
          <button key={f} className={"sx-chip" + (filter === f ? " on" : "")} onClick={() => setFilter(f)}>
            {{ all: "All", image: "Figures", table: "Tables", doc: "Reports & files" }[f]}
          </button>
        ))}
      </div>
      <div className="sx-gallery">
        {arts.map((a) => (
          <button key={a.id} className={"sx-gitem" + (a.interim ? " interim" : "")} onClick={() => onPick(a.id)} title={a.rel}>
            <div className="sx-gthumb">
              {mediaType(a.rel, a.kind) === "image"
                ? <img src={artUrl(a)} alt={a.rel} loading="lazy" />
                : <span>{kindIcon(a.kind, a.rel)}</span>}
            </div>
            <div className="sx-gcap">{a.caption || a.rel}</div>
            <div className="sx-gmeta">
              {a.interim ? "preview · " : ""}{a.by || "orchestrator"}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function Team({ st, onFocus }: { st: SciState; onFocus: (id: string) => void }) {
  const now = useNow(st.running);
  if (!st.specOrder.length) {
    return (
      <div className="sx-empty"><div className="sx-empty-ic">⚇</div>
        No specialists yet. The orchestrator dispatches them for bounded pieces of work —
        each gets its own context, tools and (optionally) model. They run in the
        background, several at once, so you can keep talking to the orchestrator while
        they work.
      </div>
    );
  }
  return (
    <div className="sx-team">
      {st.specOrder.map((id) => st.specialists[id]).filter(Boolean).map((sp) => {
        const run = sp.runs[sp.runs.length - 1];
        const working = sp.status === "working";
        const current = run ? [...run.steps].reverse().find((s) => s.ok === undefined) : undefined;
        return (
          <button key={sp.id} className={"sx-member " + sp.status} onClick={() => onFocus(sp.id)}>
            <div className="sx-member-head">
              <span className="sx-spec-ic">{sp.icon}</span>
              <div className="sx-spec-id">
                <div className="sx-spec-name">{sp.name}</div>
                <div className="sx-spec-role">{sp.role}</div>
              </div>
              <span className={"sx-pill " + sp.status}>{working ? <><span className="sx-spin sm" /> working</> : sp.status}</span>
            </div>
            {working && current && <div className="sx-member-now">{current.summary}</div>}
            {sp.goal && <div className="sx-member-goal">{sp.goal}</div>}
            {!working && sp.summary && (
              <div className="sx-member-sum"><Markdown text={sp.summary.length > 600 ? sp.summary.slice(0, 600) + "…" : sp.summary} /></div>
            )}
            <div className="sx-member-foot">
              {sp.runs.length > 0 && <span>{sp.runs.length} run{sp.runs.length === 1 ? "" : "s"}</span>}
              {run && <span>{fmtDur((run.ended || (working ? now : run.started)) - run.started)}</span>}
              {sp.model && <span>{sp.model}</span>}
            </div>
          </button>
        );
      })}
    </div>
  );
}

function Jobs({ st }: { st: SciState }) {
  if (!st.jobOrder.length) {
    return <div className="sx-empty"><div className="sx-empty-ic">⎔</div>No jobs yet. This tracks heavy computations — work submitted to a cluster, or long runs on a compute node or here. Ordinary commands the agents run are not jobs and stay out of this list.</div>;
  }
  return (
    <div className="sx-jobs">
      {st.jobOrder.slice().reverse().map((id) => st.jobs[id]).filter(Boolean).map((j) => (
        <div key={j.id} className={"sx-job " + j.status}>
          <div className="sx-job-head">
            <span className="sx-job-name">{j.name}</span>
            <span className={"sx-pill " + j.status}>
              {["submitted", "running"].includes(j.status) && <span className="sx-spin sm" />} {j.status}
            </span>
          </div>
          <div className="sx-job-meta">{j.remote_label || "local"} · {j.scheduler}{j.handle ? ` · #${j.handle}` : ""}</div>
          {j.output && <pre className="sx-code result">{j.output}</pre>}
        </div>
      ))}
    </div>
  );
}

function Provenance({ st, workingDir }: { st: SciState; workingDir: string }) {
  const ledger = viewUrl(workingDir.replace(/\/$/, "") + "/science/PROVENANCE.md");
  return (
    <div className="sx-prov">
      <a className="sx-btn sm" href={ledger} target="_blank" rel="noreferrer">Open the provenance ledger ↗</a>
      {st.prov.length === 0 ? (
        <div className="sx-empty"><div className="sx-empty-ic">⌗</div>Nothing recorded yet. Every derived result gets an entry: inputs, method, notes.</div>
      ) : (
        st.prov.slice().reverse().map((p, i) => (
          <div key={i} className="sx-prov-item">
            <div className="sx-prov-name">{p.artifact}</div>
            {p.method && <div><b>method</b> {p.method}</div>}
            {p.inputs && <div><b>inputs</b> {p.inputs}</div>}
            {p.notes && <div className="sx-muted">{p.notes}</div>}
            {p.at && <div className="sx-prov-at">{fmtWhen(p.at)}</div>}
          </div>
        ))
      )}
    </div>
  );
}
