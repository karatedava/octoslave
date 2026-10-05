import { useState } from "react";
import { ModelPicker } from "../ModelPicker";
import { Chev } from "./Feed";
import type { SessionMeta } from "./Sidebar";
import { fmtWhen, shortDir } from "./util";

export type Remote = { id: string; name?: string; host?: string };

export function Start({ connected, workingDir, setWorkingDir, models, orchModel, setOrchModel,
  pool, setPool, remotes, remoteId, setRemoteId, sessions, onStart, onOpen }: {
  connected: boolean;
  workingDir: string;
  setWorkingDir: (d: string) => void;
  models: string[];
  orchModel: string;
  setOrchModel: (m: string) => void;
  pool: string[];
  setPool: (u: (p: string[]) => string[]) => void;
  remotes: Remote[];
  remoteId: string;
  setRemoteId: (r: string) => void;
  sessions: SessionMeta[];
  onStart: (goal: string) => void;
  onOpen: (dir: string) => void;
}) {
  const [goal, setGoal] = useState("");
  const [advanced, setAdvanced] = useState(false);

  async function browse() {
    try {
      const r = await fetch("/api/pick-dir");
      const d = await r.json();
      if (d.path) setWorkingDir(d.path);
    } catch { /* no native dialog available */ }
  }

  const ready = connected && !!workingDir.trim();
  return (
    <div className="sx-start">
      <div className="sx-start-card">
        <div className="sx-start-mark">⌬</div>
        <h1>New research session</h1>
        <p className="sx-muted">
          You talk with an orchestrator; it plans, delegates to specialists, runs the
          compute and shows results live. Keep steering it while it works.
        </p>

        <label className="sx-label">Project folder</label>
        <div className="sx-row">
          <input className="sx-field" placeholder="/path/to/project — data, scripts, results live here"
            value={workingDir} onChange={(e) => setWorkingDir(e.target.value)} />
          <button className="sx-btn" type="button" onClick={browse}>Browse…</button>
        </div>

        <label className="sx-label">Goal <span className="sx-muted">(optional — you can also just start chatting)</span></label>
        <textarea className="sx-field sx-goal" rows={4} value={goal}
          placeholder="e.g. Curate the assay CSVs in data/ into one clean dataset, then plot IC50 by target and flag outliers."
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && ready) onStart(goal); }} />

        <button className="sx-disclose" onClick={() => setAdvanced((a) => !a)}>
          <Chev open={advanced} /> Models & compute
          <span className="sx-muted">
            {" "}— {orchModel || "default model"}{pool.length ? ` + ${pool.length} specialist model${pool.length > 1 ? "s" : ""}` : ""}
            {remoteId ? ` · node ${remotes.find((r) => r.id === remoteId)?.name || remoteId}` : " · local"}
          </span>
        </button>
        {advanced && (
          <div className="sx-advanced">
            <ModelPicker models={models} orchLabel="Orchestrator model" orchModel={orchModel}
              setOrchModel={setOrchModel} pool={pool} setPool={setPool} />
            {remotes.length > 0 && (
              <>
                <label className="sx-label">Compute node</label>
                <select className="sx-field" value={remoteId} onChange={(e) => setRemoteId(e.target.value)}>
                  <option value="">Local only</option>
                  {remotes.map((r) => (
                    <option key={r.id} value={r.id}>{(r.name || r.id) + (r.host ? ` (${r.host})` : "")}</option>
                  ))}
                </select>
                <div className="sx-help">
                  Everything runs locally by default. With a node, only heavy steps are sent
                  there — big files stay on it, light results (plots, projections) come back here.
                </div>
              </>
            )}
          </div>
        )}

        <button className="sx-btn primary big" disabled={!ready} onClick={() => onStart(goal)}>
          {connected ? (goal.trim() ? "Start research ↵" : "Open session") : "Connecting…"}
        </button>
      </div>

      {sessions.length > 0 && (
        <div className="sx-recent">
          <div className="sx-recent-h">Recent sessions</div>
          <div className="sx-recent-grid">
            {sessions.slice(0, 9).map((s) => (
              <button key={s.working_dir} className="sx-recent-card" onClick={() => onOpen(s.working_dir)} title={s.working_dir}>
                <div className="sx-recent-title">
                  {s.running && <span className="sx-sdot run" />}{s.title || shortDir(s.working_dir)}
                </div>
                <div className="sx-recent-dir">{shortDir(s.working_dir)}</div>
                <div className="sx-recent-when">{s.running ? "running now" : fmtWhen(s.updated_at)}</div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
