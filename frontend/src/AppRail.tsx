// The shared app rail — the SAME left navigation the Chat page has
// (octoslave/web/static/index.html), rendered with the same markup and styled by
// the same stylesheet (/static/css/rail.css, loaded in main.tsx). Keep the two
// in step: if you add a destination here, add it there too.
import { useEffect, useState, type ReactNode } from "react";

type Page = "chat" | "science" | "lab";

const ICON = { width: 22, height: 22, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
  strokeWidth: 1.75, strokeLinecap: "round" as const, strokeLinejoin: "round" as const,
  className: "nav-icon" };

const ITEMS: { page: Page | "settings"; href: string; label: string; title: string; icon: ReactNode }[] = [
  { page: "chat", href: "/", label: "Chat", title: "Chat", icon: (
    <svg {...ICON}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>) },
  { page: "science", href: "/science", label: "Science",
    title: "Science — conversational research orchestrator", icon: (
    <svg {...ICON}><path d="M9 3h6" /><path d="M10 3v6.5L5.2 17a2 2 0 0 0 1.7 3h10.2a2 2 0 0 0 1.7-3L14 9.5V3" /><path d="M7.5 15h9" /></svg>) },
  { page: "lab", href: "/lab", label: "Autonomous Research",
    title: "Autonomous Research — self-organizing agent team", icon: (
    <svg {...ICON}>
      <circle cx="12" cy="12" r="2.2" />
      <ellipse cx="12" cy="12" rx="10" ry="4.4" />
      <ellipse cx="12" cy="12" rx="10" ry="4.4" transform="rotate(60 12 12)" />
      <ellipse cx="12" cy="12" rx="10" ry="4.4" transform="rotate(120 12 12)" />
    </svg>) },
  { page: "settings", href: "/#settings", label: "Settings", title: "Settings", icon: (
    <svg {...ICON}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l-.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>) },
];

const BACKEND_LABEL: Record<string, string> = { einfra: "einfra", ollama: "local", nim: "nim" };

export function AppRail({ current, connected }: { current: Page; connected: boolean }) {
  const [status, setStatus] = useState<{ backend: string; model: string }>({ backend: "", model: "" });
  const [update, setUpdate] = useState<string>("");

  useEffect(() => {
    fetch("/api/status").then((r) => r.json()).then((d) => setStatus(d)).catch(() => {});
    // Same check the Chat page's pill makes; clicking it opens the release
    // dialog there (/#update), which knows how to run the upgrade.
    fetch("/api/update/check").then((r) => r.json())
      .then((d) => { if (d && d.available && !d.skipped && d.latest) setUpdate(d.latest); })
      .catch(() => {});
  }, []);

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <img src="/static/logo.png?v=3" alt="OctoSlave" className="brand-logo" />
        <span className="brand-name">OctoSlave</span>
      </div>
      <nav className="sidebar-nav">
        {ITEMS.map((it) => (
          <a key={it.page} className={"nav-btn nav-link" + (it.page === current ? " active" : "")}
            href={it.href} title={it.title}>
            {it.icon}
            <span>{it.label}</span>
          </a>
        ))}
      </nav>
      <div className="sidebar-bottom">
        {update && (
          <a className="update-pill" href="/#update" title={`OctoSlave ${update} is available`}
            style={{ textDecoration: "none" }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 12a9 9 0 1 1-3.5-7.1" /><polyline points="21 3 21 9 15 9" />
            </svg>
            <span>Update to {update}</span>
          </a>
        )}
        <div className="sidebar-status">
          <div className={"status-dot" + (connected ? " connected" : "")}
            title={connected ? "Connected" : "Disconnected"} />
          {status.backend && (
            <span className="backend-pill" data-backend={status.backend}>
              {BACKEND_LABEL[status.backend] || status.backend}
            </span>
          )}
          <span className="model-tag">{status.model || "—"}</span>
        </div>
      </div>
    </aside>
  );
}
