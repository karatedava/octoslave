// Shared top navigation across the OctoSlave web UIs
// (Chat / Science / Autonomous Research).
export function Nav({ current }: { current: "lab" | "science" }) {
  return (
    <nav className="appnav">
      <a className="appnav-link" href="/" title="Chat — single agent assistant">💬 Chat</a>
      <a
        className={"appnav-link" + (current === "science" ? " on" : "")}
        href="/science"
        title="Science — conversational research orchestrator"
      >
        🧬 Science
      </a>
      <a
        className={"appnav-link" + (current === "lab" ? " on" : "")}
        href="/lab"
        title="Autonomous Research — self-organizing agent team"
      >
        🧪 Autonomous Research
      </a>
    </nav>
  );
}
