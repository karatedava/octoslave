import type { Artifact } from "./store";

export const viewUrl = (absPath: string) => `/api/files/view/${encodeURIComponent(absPath)}`;
// Cache-bust so a refined output re-fetches instead of showing the cached copy.
export const artUrl = (a: Artifact, extra = 0) =>
  `${viewUrl(a.path)}?v=${a.ver || 1}${extra ? `&r=${extra}` : ""}`;

export type Media = "image" | "table" | "report" | "markdown" | "text" | "binary";

const IMG = ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"];
const TEXT = ["txt", "json", "py", "sh", "log", "yaml", "yml", "toml", "r", "fasta", "fa",
  "faa", "fna", "fastq", "pdb", "cif", "sdf", "mol2", "xyz", "gff", "gtf", "bed", "vcf",
  "tex", "bib", "rst", "ipynb"];

export function extOf(p: string): string {
  const base = p.split("/").pop() || "";
  return base.includes(".") ? (base.split(".").pop() || "").toLowerCase() : "";
}

// Pick a renderer from the file extension (robust — the emitted `kind` can be
// generic), falling back to the server-provided kind.
export function mediaType(rel: string, kind = ""): Media {
  const ext = extOf(rel);
  if (IMG.includes(ext)) return "image";
  if (ext === "csv" || ext === "tsv") return "table";
  if (ext === "html" || ext === "htm" || ext === "pdf") return "report";
  if (ext === "md") return "markdown";
  if (TEXT.includes(ext)) return "text";
  if (kind === "image") return "image";
  return "binary";
}

export const EDITABLE = /\.(md|txt|csv|tsv|json|ya?ml|html?|py|sh|svg)$/i;

export function kindIcon(kind: string, rel = ""): string {
  const m = mediaType(rel, kind);
  if (m === "image") return "🖼";
  if (m === "table") return "▦";
  if (kind === "dataset") return "🗂";
  if (m === "report") return "📄";
  if (m === "markdown") return "📝";
  return "📎";
}

const TOOL_ICONS: Record<string, string> = {
  read_file: "📄", write_file: "✍️", edit_file: "✏️", apply_patch: "🩹", bash: "⌘",
  glob: "🔎", grep: "🔎", list_dir: "📁", web_search: "🌐", web_fetch: "🌐",
  bio_inspect: "🧬", present_output: "🖼", record_provenance: "📎",
  curate_dataset: "🗂", literature_search: "📚", spawn_specialist: "🔬",
  continue_specialist: "🔁", ask_user: "❓", remember: "🧠", view_image: "👁",
  image_ocr: "🔤", pdf_ocr: "🔤", submit_cluster_job: "🖥", check_cluster_job: "🖥",
  write_cluster_file: "🖥", fetch_cluster_file: "⤓", todo_write: "☑",
  run_background: "🚀", check_process: "📡", stop_process: "🛑",
};
export const toolIcon = (name: string) => TOOL_ICONS[name] || "›";

export function shortDir(d: string): string {
  const parts = d.replace(/\/$/, "").split("/");
  return parts.slice(-2).join("/") || d;
}

export function baseName(p: string): string {
  return p.replace(/\/$/, "").split("/").pop() || p;
}

export function fmtWhen(iso: string | number): string {
  if (!iso) return "";
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  if (isNaN(d.getTime())) return "";
  const today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function fmtDur(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

// RFC-4180-ish CSV/TSV parse: quoted fields, escaped quotes, embedded separators
// and newlines. Stops after `maxRows` rows.
export function parseDelimited(text: string, maxRows: number, sep?: string): { rows: string[][]; more: boolean } {
  const nl = text.indexOf("\n");
  const firstLine = nl >= 0 ? text.slice(0, nl) : text;
  const d = sep || (firstLine.split("\t").length > firstLine.split(",").length ? "\t" : ",");
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let q = false;
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (q) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        q = false; i++; continue;
      }
      field += c; i++; continue;
    }
    if (c === '"' && field === "") { q = true; i++; continue; }
    if (c === d) { row.push(field); field = ""; i++; continue; }
    if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
      i++;
      if (rows.length > maxRows) return { rows: rows.slice(0, maxRows), more: true };
      continue;
    }
    field += c; i++;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return { rows: rows.slice(0, maxRows), more: rows.length > maxRows };
}

export function newCid(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
}
