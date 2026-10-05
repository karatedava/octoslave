// Minimal Markdown → React renderer for agent messages.
//
// Builds elements directly (never innerHTML), so model output can't inject
// markup. Covers what research chat actually uses: headings, paragraphs,
// emphasis, inline/fenced code, lists (with nesting and task boxes), quotes,
// GFM tables, rules and links. Anything else renders as plain text.
import { Fragment, type ReactNode } from "react";

type Block =
  | { t: "code"; lang: string; text: string }
  | { t: "h"; level: number; text: string }
  | { t: "p"; text: string }
  | { t: "quote"; lines: string[] }
  | { t: "list"; ordered: boolean; items: { text: string; depth: number; check?: boolean }[] }
  | { t: "table"; head: string[]; rows: string[][]; align: string[] }
  | { t: "hr" };

const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

function splitRow(line: string): string[] {
  let l = line.trim();
  if (l.startsWith("|")) l = l.slice(1);
  if (l.endsWith("|")) l = l.slice(0, -1);
  return l.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

function parse(src: string): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = line.match(/^\s*(```+|~~~+)\s*([\w+-]*)/);
    if (fence) {
      const close = fence[1];
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(close)) body.push(lines[i++]);
      i++;
      out.push({ t: "code", lang: fence[2] || "", text: body.join("\n") });
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out.push({ t: "h", level: h[1].length, text: h[2].replace(/\s+#+\s*$/, "") }); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push({ t: "hr" }); i++; continue; }
    if (/^\s*>/.test(line)) {
      const q: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push({ t: "quote", lines: q });
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      const head = splitRow(line);
      const align = splitRow(lines[i + 1]).map((c) =>
        c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "left");
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      out.push({ t: "table", head, rows, align });
      continue;
    }
    const lm = line.match(LIST_RE);
    if (lm) {
      const ordered = /\d/.test(lm[2]);
      const items: { text: string; depth: number; check?: boolean }[] = [];
      while (i < lines.length) {
        const m = lines[i].match(LIST_RE);
        if (m) {
          let text = m[3];
          let check: boolean | undefined;
          const cb = text.match(/^\[([ xX])\]\s+(.*)$/);
          if (cb) { check = cb[1] !== " "; text = cb[2]; }
          items.push({ text, depth: Math.min(3, Math.floor(m[1].replace(/\t/g, "  ").length / 2)), check });
          i++;
        } else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
          items[items.length - 1].text += " " + lines[i].trim();   // wrapped continuation
          i++;
        } else break;
      }
      out.push({ t: "list", ordered, items });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^\s*(```|~~~|#{1,6}\s|>)/.test(lines[i])
           && !LIST_RE.test(lines[i])) {
      para.push(lines[i++]);
    }
    if (!para.length) { para.push(lines[i++]); }
    out.push({ t: "p", text: para.join("\n") });
  }
  return out;
}

// Inline: `code`, **bold**, *em* / _em_, ~~strike~~, [text](url), bare URLs.
const INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|~~([\s\S]+?)~~|\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w])|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function safeHref(url: string): string | null {
  return /^(https?:|mailto:|\/|#)/i.test(url) ? url : null;
}

export function inline(text: string, keyBase = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  const pushText = (t: string) => {
    // keep single newlines inside a paragraph as line breaks
    const parts = t.split("\n");
    parts.forEach((p, j) => {
      if (j) out.push(<br key={`${keyBase}b${k++}`} />);
      if (p) out.push(p);
    });
  };
  for (const m of text.matchAll(INLINE)) {
    const idx = m.index ?? 0;
    if (idx > last) pushText(text.slice(last, idx));
    const key = `${keyBase}-${k++}`;
    if (m[1]) out.push(<code key={key} className="md-code">{m[2]}</code>);
    else if (m[3] || m[4]) out.push(<strong key={key}>{inline(m[3] || m[4], key)}</strong>);
    else if (m[5]) out.push(<del key={key}>{inline(m[5], key)}</del>);
    else if (m[6] || m[7]) out.push(<em key={key}>{inline(m[6] || m[7], key)}</em>);
    else if (m[8]) {
      const href = safeHref(m[9]);
      out.push(href
        ? <a key={key} href={href} target="_blank" rel="noreferrer">{inline(m[8], key)}</a>
        : <span key={key}>{m[8]}</span>);
    } else if (m[10]) {
      out.push(<a key={key} href={m[10]} target="_blank" rel="noreferrer">{m[10]}</a>);
    }
    last = idx + m[0].length;
  }
  if (last < text.length) pushText(text.slice(last));
  return out;
}

function List({ items, ordered }: { items: { text: string; depth: number; check?: boolean }[]; ordered: boolean }) {
  // Flat items with depth → nested lists.
  type Node = { text: string; check?: boolean; kids: Node[] };
  const root: Node[] = [];
  const stack: { depth: number; kids: Node[] }[] = [{ depth: -1, kids: root }];
  for (const it of items) {
    while (stack.length > 1 && stack[stack.length - 1].depth >= it.depth) stack.pop();
    const node: Node = { text: it.text, check: it.check, kids: [] };
    stack[stack.length - 1].kids.push(node);
    stack.push({ depth: it.depth, kids: node.kids });
  }
  const render = (nodes: Node[], top: boolean, key: string): ReactNode => {
    const Tag = ordered && top ? "ol" : "ul";
    return (
      <Tag key={key}>
        {nodes.map((n, i) => (
          <li key={i} className={n.check !== undefined ? "md-task" : undefined}>
            {n.check !== undefined && <span className={"md-check" + (n.check ? " on" : "")}>{n.check ? "✓" : ""}</span>}
            {inline(n.text, `${key}-${i}`)}
            {n.kids.length > 0 && render(n.kids, false, `${key}-${i}k`)}
          </li>
        ))}
      </Tag>
    );
  };
  return <>{render(root, true, "l")}</>;
}

export function Markdown({ text, className }: { text: string; className?: string }) {
  const blocks = parse(text || "");
  return (
    <div className={"md " + (className || "")}>
      {blocks.map((b, i) => {
        switch (b.t) {
          case "code":
            return <CodeBlock key={i} lang={b.lang} text={b.text} />;
          case "h": {
            const H = (`h${Math.min(6, b.level + 1)}`) as any;   // h1 in chat is too loud
            return <H key={i}>{inline(b.text, `h${i}`)}</H>;
          }
          case "p":
            return <p key={i}>{inline(b.text, `p${i}`)}</p>;
          case "quote":
            return <blockquote key={i}><Markdown text={b.lines.join("\n")} /></blockquote>;
          case "list":
            return <List key={i} items={b.items} ordered={b.ordered} />;
          case "hr":
            return <hr key={i} />;
          case "table":
            return (
              <div key={i} className="md-table-wrap">
                <table className="md-table">
                  <thead><tr>{b.head.map((c, j) => <th key={j} style={{ textAlign: b.align[j] as any }}>{inline(c, `t${i}h${j}`)}</th>)}</tr></thead>
                  <tbody>
                    {b.rows.map((r, ri) => (
                      <tr key={ri}>{b.head.map((_, j) => <td key={j} style={{ textAlign: b.align[j] as any }}>{inline(r[j] || "", `t${i}r${ri}c${j}`)}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
        }
        return <Fragment key={i} />;
      })}
    </div>
  );
}

function CodeBlock({ lang, text }: { lang: string; text: string }) {
  const copy = () => { try { navigator.clipboard.writeText(text); } catch { /* ignore */ } };
  return (
    <div className="md-pre">
      <div className="md-pre-bar">
        <span>{lang || "text"}</span>
        <button onClick={copy} title="Copy">Copy</button>
      </div>
      <pre><code>{text}</code></pre>
    </div>
  );
}
