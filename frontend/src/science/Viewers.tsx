// File renderers shared by the conversation's output cards ("compact") and the
// workspace preview ("full").
import { useEffect, useState } from "react";
import { Markdown } from "./Markdown";
import { mediaType, parseDelimited } from "./util";

type Size = "compact" | "full";

export function FileView({ url, rel, kind = "", size }: {
  url: string; rel: string; kind?: string; size: Size;
}) {
  const media = mediaType(rel, kind);
  if (media === "image") return <ImageView url={url} alt={rel} size={size} />;
  if (media === "table") return <TableView url={url} size={size} />;
  if (media === "report") {
    return <iframe className={"sx-frame " + size} src={url} title={rel} />;
  }
  if (media === "markdown") return <TextView url={url} size={size} markdown />;
  if (media === "text") return <TextView url={url} size={size} />;
  return (
    <div className="sx-noprev">
      No inline preview for this file type — <a href={url} target="_blank" rel="noreferrer">open it ↗</a>
    </div>
  );
}

function ImageView({ url, alt, size }: { url: string; alt: string; size: Size }) {
  const [failed, setFailed] = useState(false);
  const [zoom, setZoom] = useState(false);
  useEffect(() => { setFailed(false); }, [url]);
  if (failed) {
    return (
      <div className="sx-noprev">
        Couldn't render this image — <a href={url} target="_blank" rel="noreferrer">open it ↗</a>
      </div>
    );
  }
  return (
    <div className={"sx-img-wrap " + size + (zoom ? " zoom" : "")}
      onClick={size === "full" ? () => setZoom((z) => !z) : undefined}
      title={size === "full" ? (zoom ? "Fit to panel" : "Actual size") : undefined}>
      <img src={url} alt={alt} onError={() => setFailed(true)} draggable={false} />
    </div>
  );
}

function useText(url: string, limit = 400_000) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    setErr("");
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((t) => { if (alive) setText(t.length > limit ? t.slice(0, limit) + "\n…(truncated)" : t); })
      .catch((e) => { if (alive) setErr(String(e.message || e)); });
    return () => { alive = false; };
  }, [url, limit]);
  return { text, err };
}

function TableView({ url, size }: { url: string; size: Size }) {
  const { text, err } = useText(url, 3_000_000);
  if (err) return <div className="sx-noprev">Couldn't load table ({err}).</div>;
  if (text === null) return <div className="sx-loading">Loading table…</div>;
  const max = size === "compact" ? 9 : 1000;
  const { rows, more } = parseDelimited(text, max + 1);
  if (!rows.length) return <div className="sx-noprev">Empty table.</div>;
  const [head, ...body] = rows;
  const cols = size === "compact" ? Math.min(head.length, 8) : head.length;
  return (
    <div className={"sx-table-wrap " + size}>
      <table className="sx-table">
        <thead><tr><th className="rn">#</th>{head.slice(0, cols).map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i}>
              <td className="rn">{i + 1}</td>
              {head.slice(0, cols).map((_, j) => {
                const v = r[j] ?? "";
                return <td key={j} className={/^-?[\d.,eE+-]+%?$/.test(v) ? "num" : ""}>{v}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {(more || cols < head.length) && (
        <div className="sx-table-more">
          {more ? `First ${body.length} rows` : `${body.length} rows`}
          {cols < head.length ? ` · ${cols} of ${head.length} columns` : ""}
          {size === "compact" ? " — open in the workspace for more" : ""}
        </div>
      )}
    </div>
  );
}

function TextView({ url, size, markdown }: { url: string; size: Size; markdown?: boolean }) {
  const { text, err } = useText(url);
  if (err) return <div className="sx-noprev">Couldn't load file ({err}).</div>;
  if (text === null) return <div className="sx-loading">Loading…</div>;
  const shown = size === "compact" ? text.split("\n").slice(0, 14).join("\n") : text;
  if (markdown) {
    return <div className={"sx-doc " + size}><Markdown text={shown} /></div>;
  }
  return <pre className={"sx-textview " + size}>{shown}</pre>;
}
