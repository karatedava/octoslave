// Prompt for a pending `ask_user` call.
//
// When an agent calls ask_user the server emits a `user_question` event and the
// agent thread BLOCKS inside the tool until an answer arrives (or the server's
// timeout fires and it proceeds on its own judgement). Neither the Science nor
// the Lab composer can serve as the reply box — both are locked while a turn is
// in flight — so the question needs its own always-visible input, or it simply
// cannot be answered.
import { useEffect, useRef, useState } from "react";

// `expires` is an absolute timestamp derived from the server's own timeout, so
// the countdown reflects when the agent will actually give up rather than a
// number the UI invented.
export type Ask = { question: string; options: string[]; expires: number };

export function askFromEvent(m: any): Ask {
  return {
    question: m.question || "",
    options: Array.isArray(m.options) ? m.options : [],
    expires: Date.now() + (Number(m.timeout) || 600) * 1000,
  };
}

export function AskCard({ ask, onAnswer }: { ask: Ask; onAnswer: (t: string) => void }) {
  const [text, setText] = useState("");
  const [left, setLeft] = useState(() => Math.max(0, ask.expires - Date.now()));
  const box = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setText("");
    box.current?.focus();
  }, [ask.question]);

  useEffect(() => {
    const t = window.setInterval(
      () => setLeft(Math.max(0, ask.expires - Date.now())), 1000);
    return () => clearInterval(t);
  }, [ask.expires]);

  const mins = Math.floor(left / 60000);
  const secs = Math.floor((left % 60000) / 1000);

  return (
    <div className="sci-ask">
      <div className="sci-ask-head">
        <span className="sci-ask-tag">❓ Waiting on your answer</span>
        <span className={"sci-ask-clock" + (left < 60000 ? " urgent" : "")}>
          {left > 0
            ? `${mins}:${String(secs).padStart(2, "0")} left`
            : "expired — carrying on without an answer"}
        </span>
      </div>
      <div className="sci-ask-q">{ask.question}</div>
      {ask.options.length > 0 && (
        <div className="sci-ask-opts">
          {ask.options.map((o) => (
            <button key={o} className="btn" onClick={() => onAnswer(o)}>{o}</button>
          ))}
        </div>
      )}
      <div className="sci-ask-row">
        <input
          ref={box}
          className="inject-input"
          placeholder="Type your answer…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onAnswer(text)}
        />
        <button className="btn primary" disabled={!text.trim()}
          onClick={() => onAnswer(text)}>Answer</button>
      </div>
      <div className="sci-ask-hint">
        It resumes the turn already in flight — this is not a new message.
      </div>
    </div>
  );
}
