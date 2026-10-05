import { useCallback, useEffect, useState } from "react";
import { Chev } from "./Feed";
import { fmtSize, fmtWhen, shortDir } from "./util";

export type SessionMeta = {
  working_dir: string; title: string; created_at: string; updated_at: string; running?: boolean;
};

type Entry = { name: string; rel: string; path: string; dir: boolean; size: number; mtime: number };

export function Sidebar({ sessions, current, onOpen, onNew, onForget, workingDir, refreshKey,
  onOpenFile, activeFile }: {
  sessions: SessionMeta[];
  current: string;
  onOpen: (dir: string) => void;
  onNew: () => void;
  onForget: (dir: string) => void;
  workingDir: string;
  refreshKey: number;
  onOpenFile: (path: string, rel: string) => void;
  activeFile: string;
}) {
  const [showSessions, setShowSessions] = useState(true);
  const [showFiles, setShowFiles] = useState(true);
  return (
    <aside className="sx-side">
      <div className="sx-side-sec">
        <div className="sx-side-h">
          <button className="sx-side-toggle" onClick={() => setShowSessions((v) => !v)}>
            <Chev open={showSessions} /> Sessions
          </button>
          <button className="sx-icon-btn" title="New session" onClick={onNew}>＋</button>
        </div>
        {showSessions && (
          <div className="sx-sessions">
            {sessions.length === 0 && <div className="sx-side-empty">No sessions yet.</div>}
            {sessions.map((s) => (
              <div key={s.working_dir}
                className={"sx-session" + (s.working_dir === current ? " on" : "")}
                onClick={() => onOpen(s.working_dir)} title={s.working_dir}>
                <span className={"sx-sdot" + (s.running ? " run" : "")} />
                <div className="sx-session-main">
                  <div className="sx-session-title">{s.title || shortDir(s.working_dir)}</div>
                  <div className="sx-session-dir">{shortDir(s.working_dir)} · {fmtWhen(s.updated_at)}</div>
                </div>
                <button className="sx-x" title="Remove from history (keeps the files)"
                  onClick={(e) => { e.stopPropagation(); onForget(s.working_dir); }}>×</button>
              </div>
            ))}
          </div>
        )}
      </div>
      {workingDir && (
        <div className="sx-side-sec grow">
          <div className="sx-side-h">
            <button className="sx-side-toggle" onClick={() => setShowFiles((v) => !v)}>
              <Chev open={showFiles} /> Files
            </button>
          </div>
          {showFiles && (
            <div className="sx-tree">
              <Dir workingDir={workingDir} rel="" depth={0} refreshKey={refreshKey}
                onOpenFile={onOpenFile} activeFile={activeFile} />
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

function fileIcon(name: string): string {
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "svg", "webp"].includes(ext)) return "🖼";
  if (["csv", "tsv", "xlsx", "parquet"].includes(ext)) return "▦";
  if (["py", "r", "sh", "ipynb", "js", "ts"].includes(ext)) return "⌘";
  if (["md", "txt", "rst", "tex"].includes(ext)) return "📝";
  if (["html", "htm", "pdf"].includes(ext)) return "📄";
  if (["fasta", "fa", "fastq", "pdb", "cif", "sdf", "vcf", "h5ad", "mol2"].includes(ext)) return "🧬";
  if (["json", "yaml", "yml", "toml"].includes(ext)) return "{}";
  return "·";
}

function Dir({ workingDir, rel, depth, refreshKey, onOpenFile, activeFile }: {
  workingDir: string; rel: string; depth: number; refreshKey: number;
  onOpenFile: (path: string, rel: string) => void; activeFile: string;
}) {
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});

  const load = useCallback(() => {
    fetch(`/api/science/files?working_dir=${encodeURIComponent(workingDir)}&path=${encodeURIComponent(rel)}`)
      .then((r) => r.json())
      .then((d) => {
        if (d.ok) { setEntries(d.entries); setErr(""); } else { setErr(d.error || "unavailable"); }
      })
      .catch(() => setErr("unavailable"));
  }, [workingDir, rel]);

  useEffect(() => { load(); }, [load, refreshKey]);

  if (err) return <div className="sx-tree-empty" style={{ paddingLeft: 12 + depth * 12 }}>{err}</div>;
  if (!entries) return <div className="sx-tree-empty" style={{ paddingLeft: 12 + depth * 12 }}>…</div>;
  if (!entries.length) return <div className="sx-tree-empty" style={{ paddingLeft: 12 + depth * 12 }}>empty</div>;
  return (
    <>
      {entries.map((e) => (
        <div key={e.rel}>
          <div className={"sx-node" + (e.dir ? " dir" : "") + (activeFile === e.path ? " on" : "")}
            style={{ paddingLeft: 10 + depth * 12 }}
            onClick={() => (e.dir ? setOpen((o) => ({ ...o, [e.rel]: !o[e.rel] })) : onOpenFile(e.path, e.rel))}
            title={e.dir ? e.rel : `${e.rel} · ${fmtSize(e.size)}`}>
            <span className="sx-node-ic">{e.dir ? <Chev open={!!open[e.rel]} /> : fileIcon(e.name)}</span>
            <span className="sx-node-name">{e.name}</span>
          </div>
          {e.dir && open[e.rel] && (
            <Dir workingDir={workingDir} rel={e.rel} depth={depth + 1} refreshKey={refreshKey}
              onOpenFile={onOpenFile} activeFile={activeFile} />
          )}
        </div>
      ))}
    </>
  );
}
