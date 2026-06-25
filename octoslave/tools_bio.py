"""
Domain-specialised tools for biology and chemistry.

These are first-class agent tools (not shelled out via bash). They cover:

  Schema-aware loaders:
    bio_inspect       FASTA / FASTQ / VCF / GFF / GTF / PDB / mmCIF / MTX /
                      h5ad / SMI / SDF — returns a structured summary.

  Cheminformatics:
    rdkit_describe    SMILES -> canonical SMILES, MW, logP, TPSA, HBD/HBA,
                      rings, rotatable bonds, formula, QED, Lipinski.

  Public-DB connectors (replace ad-hoc web_fetch routing):
    uniprot_lookup    UniProtKB protein record / search.
    pubchem_lookup    PubChem compound by CID / name / SMILES.
    chembl_lookup     ChEMBL bioactive molecule record / name search.
    pdb_fetch         RCSB structure (PDB / mmCIF) download by ID.
    alphafold_fetch   AlphaFold DB predicted structure by UniProt accession.
    geo_search        NCBI GEO / SRA datasets via E-utilities.
    ena_fetch         EBI ENA file report (FASTQ download URLs etc.).

  PDF rescue:
    pdf_ocr           Render PDF pages to images and OCR them — recovers numbers
                      and labels embedded in figures (axis ticks, EC50 values,
                      heat-map legends) that pypdf's text extractor cannot see.

All heavy deps (Biopython, RDKit, anndata, scipy.io) are imported lazily.
If a dep is missing, the tool returns a friendly "pip install X" message
rather than crashing the agent.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# ---------------------------------------------------------------------------
# Tool schemas (appended to TOOL_DEFINITIONS in tools.py)
# ---------------------------------------------------------------------------

BIO_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bio_inspect",
            "description": (
                "Schema-aware preview of a data table or biological / chemical "
                "data file. Auto-detects format (CSV, TSV, Parquet, JSONL, FASTA, "
                "FASTQ, VCF, GFF/GTF, PDB, mmCIF, Matrix Market .mtx, AnnData "
                ".h5ad, SMILES .smi/.smiles, SDF). For tables it returns shape, "
                "columns+dtypes, a head sample, a numeric summary and missing-value "
                "counts (computed on a bounded row sample, so it's fast on multi-GB "
                "files). ALWAYS use this to inspect a dataset instead of read_file — "
                "read_file just dumps a truncated, unusable head of a large file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the data file"},
                    "format": {
                        "type": "string",
                        "description": (
                            "Optional override. One of: table, fasta, fastq, vcf, "
                            "gff, gtf, pdb, cif, mtx, h5ad, smi, sdf. Auto-detected "
                            "from extension if omitted (use 'table' for CSV/TSV/"
                            "Parquet/JSONL)."
                        ),
                    },
                    "head": {
                        "type": "integer",
                        "description": "Number of records to preview (default 3, max 50)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rdkit_describe",
            "description": (
                "Compute physicochemical / drug-likeness properties for a SMILES "
                "string using RDKit: canonical SMILES, MW, logP (Crippen), TPSA, "
                "H-bond donors/acceptors, rotatable bonds, ring count, aromatic "
                "rings, heavy atom count, molecular formula, QED, Lipinski "
                "Rule-of-5 violations. Use for any small-molecule analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {"type": "string", "description": "SMILES string"},
                },
                "required": ["smiles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uniprot_lookup",
            "description": (
                "Query UniProtKB. Pass accession (e.g. 'P12345') for a specific "
                "entry, or query (e.g. 'lysozyme human reviewed:true') to search. "
                "Returns: accession, name, organism, length, sequence (first 200 aa), "
                "function, GO terms, cross-refs to PDB/AlphaFold. "
                "Prefer this over web_fetch for any UniProt lookup."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accession": {"type": "string", "description": "UniProt accession (e.g. P12345)"},
                    "query": {"type": "string", "description": "Free-text UniProt query"},
                    "limit": {"type": "integer", "description": "Max results for query (default 5, max 25)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pubchem_lookup",
            "description": (
                "Query PubChem by name, CID, or SMILES. Returns CID, canonical SMILES, "
                "InChI, IUPAC name, MW, formula, XLogP, HBD/HBA, TPSA, "
                "and rotatable bond count. Prefer this over web_fetch for any small-"
                "molecule identification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Compound name (e.g. 'aspirin')"},
                    "cid": {"type": "integer", "description": "PubChem Compound ID"},
                    "smiles": {"type": "string", "description": "SMILES string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chembl_lookup",
            "description": (
                "Query ChEMBL for a bioactive molecule. Pass chembl_id (e.g. "
                "'CHEMBL25') or name. Returns: ChEMBL ID, preferred name, max "
                "phase, molecule type, MW, ALogP, RO5 violations, indications, "
                "first reported activity targets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chembl_id": {"type": "string", "description": "ChEMBL molecule ID"},
                    "name": {"type": "string", "description": "Drug / compound name"},
                    "limit": {"type": "integer", "description": "Max results for name search (default 5)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pdb_fetch",
            "description": (
                "Download a structure from the RCSB Protein Data Bank by 4-character "
                "PDB ID. Returns header summary (title, resolution, chains, "
                "length, ligands) and saves the full file to output_path (or "
                "{pdb_id}.{format} in working dir if omitted)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pdb_id": {"type": "string", "description": "4-character PDB ID (e.g. '1CRN')"},
                    "format": {"type": "string", "description": "'pdb' or 'cif' (default 'pdb')"},
                    "output_path": {"type": "string", "description": "Where to save the structure file"},
                },
                "required": ["pdb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alphafold_fetch",
            "description": (
                "Download a predicted protein structure from AlphaFold DB by "
                "UniProt accession. Returns mean pLDDT and saves PDB to "
                "output_path (or AF-{uniprot_id}.pdb in working dir)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uniprot_id": {"type": "string", "description": "UniProt accession (e.g. 'P12345')"},
                    "output_path": {"type": "string", "description": "Where to save the PDB"},
                },
                "required": ["uniprot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "geo_search",
            "description": (
                "Search NCBI GEO / SRA via E-utilities. Pass a GEO/GDS accession "
                "(e.g. 'GSE10072') for a specific study, or a free-text query "
                "(e.g. 'pancreatic cancer scRNA-seq'). Returns study accessions, "
                "titles, organism, sample count, platform, summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accession": {"type": "string", "description": "GEO Series / Dataset accession"},
                    "query": {"type": "string", "description": "Free-text query"},
                    "limit": {"type": "integer", "description": "Max results (default 5, max 25)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pdf_ocr",
            "description": (
                "Render PDF pages to images and run OCR on them — recovers text "
                "embedded in figures (axis ticks, EC50/IC50 values, heat-map "
                "legends, rasterised data tables) that pypdf's text extractor "
                "cannot see. Use this AFTER read_file on a PDF if the answer "
                "you need is in a figure (e.g. 'value in Figure 1C'). Requires "
                "PyMuPDF (`pip install pymupdf`) for rendering and pytesseract "
                "+ the `tesseract` binary for OCR. Without tesseract the tool "
                "still extracts page images for inspection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to PDF file"},
                    "pages": {
                        "type": "string",
                        "description": (
                            "Page selection: '1', '1-5', '1,3,5', or 'all' (default 'all'). "
                            "1-indexed."
                        ),
                    },
                    "dpi": {
                        "type": "integer",
                        "description": "Render DPI (default 200; raise to 300 for small text)",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": (
                            "Directory to save the rendered page PNGs (relative to working dir). "
                            "If omitted, images are not persisted — only OCR text is returned."
                        ),
                    },
                    "lang": {
                        "type": "string",
                        "description": "Tesseract language code (default 'eng')",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ena_fetch",
            "description": (
                "Fetch an ENA / SRA file report by accession (study, sample, "
                "experiment, or run, e.g. 'PRJNA123456' / 'SRR1234567'). Returns "
                "FASTQ download URLs, read counts, library layout, and sample "
                "metadata. Use BEFORE attempting to download sequencing data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accession": {"type": "string", "description": "ENA / SRA accession"},
                    "result": {
                        "type": "string",
                        "description": "ENA result type: 'read_run' (default), 'study', 'sample'",
                    },
                },
                "required": ["accession"],
            },
        },
    },
]


BIO_TOOL_NAMES = frozenset(t["function"]["name"] for t in BIO_TOOL_DEFINITIONS)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def execute_bio_tool(name: str, args: dict, working_dir: str) -> tuple[str, bool]:
    """Returns (result_text, success). Returns (None, None) if `name` is not a bio tool."""
    if name not in BIO_TOOL_NAMES:
        return None, None
    try:
        if name == "bio_inspect":
            return _bio_inspect(working_dir=working_dir, **args)
        if name == "rdkit_describe":
            return _rdkit_describe(**args)
        if name == "uniprot_lookup":
            return _uniprot_lookup(**args)
        if name == "pubchem_lookup":
            return _pubchem_lookup(**args)
        if name == "chembl_lookup":
            return _chembl_lookup(**args)
        if name == "pdb_fetch":
            return _pdb_fetch(working_dir=working_dir, **args)
        if name == "alphafold_fetch":
            return _alphafold_fetch(working_dir=working_dir, **args)
        if name == "geo_search":
            return _geo_search(**args)
        if name == "ena_fetch":
            return _ena_fetch(**args)
        if name == "pdf_ocr":
            return _pdf_ocr(working_dir=working_dir, **args)
    except TypeError as e:
        return f"Invalid arguments for {name}: {e}", False
    except Exception as e:
        return f"Tool error ({name}): {e}", False
    return f"Unknown bio tool: {name}", False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(path: str, working_dir: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(working_dir) / p
    return p.resolve()


def _need_requests() -> tuple[str, bool] | None:
    if not _HAS_REQUESTS:
        return ("`requests` is not installed. Run: pip install requests", False)
    return None


def _http_get(url: str, params: dict = None, timeout: int = 30, accept: str = "application/json"):
    headers = {"Accept": accept, "User-Agent": "OctoSlave/0.1 (research agent)"}
    return _requests.get(url, params=params or {}, headers=headers, timeout=timeout)


# ---------------------------------------------------------------------------
# bio_inspect — schema-aware preview
# ---------------------------------------------------------------------------

_FORMAT_BY_EXT = {
    ".fasta": "fasta", ".fa": "fasta", ".fna": "fasta", ".faa": "fasta", ".ffn": "fasta",
    ".fastq": "fastq", ".fq": "fastq",
    ".vcf": "vcf",
    ".gff": "gff", ".gff3": "gff",
    ".gtf": "gtf",
    ".pdb": "pdb", ".ent": "pdb",
    ".cif": "cif", ".mmcif": "cif",
    ".mtx": "mtx",
    ".h5ad": "h5ad",
    ".smi": "smi", ".smiles": "smi",
    ".sdf": "sdf", ".mol": "sdf",
    # tabular / scientific data tables — schema-aware preview instead of a raw dump
    ".csv": "table", ".tsv": "table", ".tab": "table",
    ".parquet": "table", ".pq": "table",
    ".jsonl": "table", ".ndjson": "table",
}


def _bio_inspect(path: str, working_dir: str, format: str = None, head: int = 3) -> tuple[str, bool]:
    resolved = _resolve(path, working_dir)
    if not resolved.exists():
        return f"File not found: {resolved}", False
    if not resolved.is_file():
        return f"Not a file: {resolved}", False

    head = max(1, min(int(head or 3), 50))
    fmt = (format or "").lower().strip()
    if not fmt:
        # honour double extensions like .fasta.gz / .vcf.gz
        suffixes = [s.lower() for s in resolved.suffixes]
        if suffixes and suffixes[-1] in (".gz", ".bgz", ".bz2", ".xz"):
            suffixes = suffixes[:-1]
        ext = suffixes[-1] if suffixes else ""
        fmt = _FORMAT_BY_EXT.get(ext, "")

    if not fmt:
        return (
            f"Could not auto-detect format for {resolved.name}. Pass format="
            f"<fasta|fastq|vcf|gff|gtf|pdb|cif|mtx|h5ad|smi|sdf> explicitly.",
            False,
        )

    handlers = {
        "fasta": _inspect_fasta,
        "fastq": _inspect_fastq,
        "vcf": _inspect_vcf,
        "gff": _inspect_gff_gtf,
        "gtf": _inspect_gff_gtf,
        "pdb": _inspect_pdb,
        "cif": _inspect_cif,
        "mtx": _inspect_mtx,
        "h5ad": _inspect_h5ad,
        "smi": _inspect_smi,
        "sdf": _inspect_sdf,
        "table": _inspect_table,
    }
    return handlers[fmt](resolved, head, fmt)


def _open_text(path: Path):
    """Open plain or gzip-compressed text file as a line iterator."""
    if path.suffix.lower() in (".gz", ".bgz"):
        import gzip
        return gzip.open(path, "rt", errors="replace")
    return open(path, "r", errors="replace")


def _inspect_fasta(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    n = 0
    lengths: list[int] = []
    preview: list[tuple[str, int, str]] = []
    cur_id = None
    cur_len = 0
    cur_seq_head = ""
    try:
        with _open_text(path) as f:
            for line in f:
                if line.startswith(">"):
                    if cur_id is not None:
                        lengths.append(cur_len)
                        if len(preview) < head:
                            preview.append((cur_id, cur_len, cur_seq_head[:60]))
                    cur_id = line[1:].strip().split()[0] if len(line) > 1 else ""
                    cur_len = 0
                    cur_seq_head = ""
                    n += 1
                else:
                    s = line.strip()
                    cur_len += len(s)
                    if len(cur_seq_head) < 60:
                        cur_seq_head += s
            if cur_id is not None:
                lengths.append(cur_len)
                if len(preview) < head:
                    preview.append((cur_id, cur_len, cur_seq_head[:60]))
    except OSError as e:
        return f"Read error: {e}", False

    if not lengths:
        return f"FASTA {path.name}: no records found", False

    summary = {
        "format": "FASTA",
        "file": path.name,
        "records": n,
        "length_min": min(lengths),
        "length_max": max(lengths),
        "length_mean": sum(lengths) / len(lengths),
        "preview": [{"id": i, "length": L, "seq_head": s} for i, L, s in preview],
    }
    return json.dumps(summary, indent=2), True


def _inspect_fastq(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    n = 0
    lengths: list[int] = []
    preview = []
    try:
        with _open_text(path) as f:
            while True:
                hdr = f.readline()
                if not hdr:
                    break
                seq = f.readline().rstrip("\n")
                _plus = f.readline()
                _qual = f.readline()
                if not seq:
                    break
                n += 1
                lengths.append(len(seq))
                if len(preview) < head:
                    preview.append({"id": hdr.lstrip("@").strip().split()[0], "length": len(seq), "seq_head": seq[:60]})
                if n >= 1_000_000:  # cap
                    break
    except OSError as e:
        return f"Read error: {e}", False
    if not lengths:
        return f"FASTQ {path.name}: no reads found", False
    cap = " (capped at 1M reads)" if n >= 1_000_000 else ""
    return json.dumps({
        "format": "FASTQ",
        "file": path.name,
        "reads": f"{n}{cap}",
        "length_min": min(lengths),
        "length_max": max(lengths),
        "length_mean": round(sum(lengths) / len(lengths), 2),
        "preview": preview,
    }, indent=2), True


def _inspect_vcf(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    samples: list[str] = []
    info_keys: set[str] = set()
    chroms: dict[str, int] = {}
    n_variants = 0
    preview = []
    try:
        with _open_text(path) as f:
            for line in f:
                if line.startswith("##INFO=<ID="):
                    key = line.split("ID=", 1)[1].split(",", 1)[0]
                    info_keys.add(key)
                elif line.startswith("#CHROM"):
                    fields = line.rstrip("\n").split("\t")
                    samples = fields[9:] if len(fields) > 9 else []
                elif line.startswith("#"):
                    continue
                else:
                    n_variants += 1
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 5:
                        chrom, pos, vid, ref, alt = parts[:5]
                        chroms[chrom] = chroms.get(chrom, 0) + 1
                        if len(preview) < head:
                            preview.append({"chrom": chrom, "pos": pos, "id": vid, "ref": ref, "alt": alt})
                    if n_variants >= 500_000:
                        break
    except OSError as e:
        return f"Read error: {e}", False
    cap = " (capped at 500k)" if n_variants >= 500_000 else ""
    return json.dumps({
        "format": "VCF",
        "file": path.name,
        "variants": f"{n_variants}{cap}",
        "samples": samples,
        "n_samples": len(samples),
        "chromosomes": chroms,
        "info_keys": sorted(info_keys),
        "preview": preview,
    }, indent=2), True


def _inspect_gff_gtf(path: Path, head: int, fmt: str) -> tuple[str, bool]:
    types: dict[str, int] = {}
    sources: dict[str, int] = {}
    seqids: set[str] = set()
    n = 0
    preview = []
    try:
        with _open_text(path) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 9:
                    continue
                seqid, source, ftype = parts[0], parts[1], parts[2]
                types[ftype] = types.get(ftype, 0) + 1
                sources[source] = sources.get(source, 0) + 1
                seqids.add(seqid)
                n += 1
                if len(preview) < head:
                    preview.append({
                        "seqid": seqid, "source": source, "type": ftype,
                        "start": parts[3], "end": parts[4], "strand": parts[6],
                        "attrs": parts[8][:120],
                    })
                if n >= 1_000_000:
                    break
    except OSError as e:
        return f"Read error: {e}", False
    cap = " (capped at 1M)" if n >= 1_000_000 else ""
    return json.dumps({
        "format": fmt.upper(),
        "file": path.name,
        "features": f"{n}{cap}",
        "feature_types": types,
        "sources": sources,
        "seqid_count": len(seqids),
        "preview": preview,
    }, indent=2), True


def _inspect_pdb(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    title_parts = []
    chains: dict[str, int] = {}
    hetatms: set[str] = set()
    resolution = None
    classification = None
    try:
        with _open_text(path) as f:
            for line in f:
                rec = line[:6].rstrip()
                if rec == "TITLE":
                    title_parts.append(line[10:80].strip())
                elif rec == "HEADER":
                    classification = line[10:50].strip()
                elif rec == "REMARK" and line.startswith("REMARK   2 RESOLUTION."):
                    try:
                        resolution = float(line.split()[3])
                    except (ValueError, IndexError):
                        pass
                elif rec == "ATOM":
                    chain = line[21]
                    chains[chain] = chains.get(chain, 0) + 1
                elif rec == "HETATM":
                    res = line[17:20].strip()
                    if res not in ("HOH", "WAT"):
                        hetatms.add(res)
    except OSError as e:
        return f"Read error: {e}", False
    return json.dumps({
        "format": "PDB",
        "file": path.name,
        "title": " ".join(title_parts).strip()[:200] or None,
        "classification": classification,
        "resolution_A": resolution,
        "chains": {c: f"{n} atoms" for c, n in chains.items()},
        "hetatm_residues": sorted(hetatms),
    }, indent=2), True


def _inspect_cif(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    info: dict[str, str] = {}
    chain_set: set[str] = set()
    try:
        with _open_text(path) as f:
            for line in f:
                if line.startswith("_struct.title"):
                    info["title"] = line.split(maxsplit=1)[1].strip().strip("'\"") if " " in line else ""
                elif line.startswith("_entry.id"):
                    info["entry_id"] = line.split()[1] if len(line.split()) > 1 else ""
                elif line.startswith("_reflns.d_resolution_high"):
                    parts = line.split()
                    if len(parts) > 1:
                        info["resolution_high"] = parts[1]
    except OSError as e:
        return f"Read error: {e}", False
    return json.dumps({"format": "mmCIF", "file": path.name, **info}, indent=2), True


def _inspect_mtx(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    try:
        from scipy.io import mmread
    except ImportError:
        return "scipy is not installed. Run: pip install scipy", False
    try:
        mat = mmread(str(path))
    except Exception as e:
        return f"mmread error: {e}", False
    nrows, ncols = mat.shape
    nnz = mat.nnz if hasattr(mat, "nnz") else int((mat != 0).sum())
    density = nnz / (nrows * ncols) if nrows * ncols else 0.0
    coo = mat.tocoo()
    sample = [
        {"row": int(coo.row[i]), "col": int(coo.col[i]), "value": float(coo.data[i])}
        for i in range(min(head, len(coo.data)))
    ]
    return json.dumps({
        "format": "Matrix Market",
        "file": path.name,
        "shape": [nrows, ncols],
        "nnz": nnz,
        "density": density,
        "preview": sample,
    }, indent=2), True


def _inspect_h5ad(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    try:
        import anndata as ad
    except ImportError:
        return "anndata is not installed. Run: pip install anndata", False
    try:
        a = ad.read_h5ad(str(path), backed="r")
    except Exception as e:
        return f"anndata read error: {e}", False
    summary = {
        "format": "AnnData (h5ad)",
        "file": path.name,
        "n_obs": int(a.n_obs),
        "n_vars": int(a.n_vars),
        "obs_keys": list(a.obs.columns)[:50],
        "var_keys": list(a.var.columns)[:50],
        "obsm_keys": list(a.obsm.keys()) if hasattr(a, "obsm") else [],
        "varm_keys": list(a.varm.keys()) if hasattr(a, "varm") else [],
        "layers": list(a.layers.keys()) if hasattr(a, "layers") else [],
        "uns_keys": list(a.uns.keys())[:20] if hasattr(a, "uns") else [],
        "obs_head": a.obs.head(head).to_dict(orient="records") if a.n_obs else [],
    }
    try:
        a.file.close()
    except Exception:
        pass
    return json.dumps(summary, indent=2, default=str), True


def _inspect_smi(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    rows = []
    n = 0
    try:
        with _open_text(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                n += 1
                if len(rows) < head:
                    parts = line.split(None, 1)
                    rows.append({"smiles": parts[0], "name": parts[1] if len(parts) > 1 else None})
                if n >= 5_000_000:
                    break
    except OSError as e:
        return f"Read error: {e}", False
    cap = " (capped at 5M)" if n >= 5_000_000 else ""
    return json.dumps({
        "format": "SMILES (.smi)",
        "file": path.name,
        "molecules": f"{n}{cap}",
        "preview": rows,
    }, indent=2), True


def _inspect_sdf(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    n = 0
    prop_keys: set[str] = set()
    preview = []
    try:
        with _open_text(path) as f:
            block_lines: list[str] = []
            for line in f:
                if line.startswith("$$$$"):
                    n += 1
                    if len(preview) < head and block_lines:
                        title = block_lines[0].strip() if block_lines else ""
                        preview.append({"title": title, "lines": len(block_lines)})
                    # collect property keys (lines starting with "> <KEY>")
                    for bl in block_lines:
                        if bl.startswith("> <") and ">" in bl[3:]:
                            key = bl[3:].split(">", 1)[0]
                            prop_keys.add(key)
                    block_lines = []
                    if n >= 1_000_000:
                        break
                else:
                    if len(block_lines) < 200:
                        block_lines.append(line)
                    else:
                        block_lines.append("")
    except OSError as e:
        return f"Read error: {e}", False
    return json.dumps({
        "format": "SDF",
        "file": path.name,
        "molecules": n,
        "property_keys": sorted(prop_keys)[:50],
        "preview": preview,
    }, indent=2), True


# Read at most this many rows to infer dtypes / compute the numeric summary, so
# bio_inspect stays fast and bounded even on multi-GB tables (the FULL file is
# never loaded into the model's context — only this compact schema digest is).
_TABLE_SUMMARY_ROWS = 50_000


def _count_lines(path: Path) -> int:
    """Fast newline count without parsing — cheap even for multi-GB files."""
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            total += chunk.count(b"\n")
    return total


def _inspect_table(path: Path, head: int, _fmt: str) -> tuple[str, bool]:
    """Schema-aware preview for CSV/TSV/Parquet/JSONL tables.

    Returns shape, per-column dtypes, a head sample, and a numeric describe()
    computed on a bounded row sample — a compact digest the model can plan on,
    instead of read_file dumping (and trying to number) millions of lines.
    """
    try:
        import pandas as pd
    except ImportError:
        return "pandas is not installed. Run: pip install pandas", False

    ext = path.suffix.lower()
    head = max(1, min(int(head or 5), 50))
    sampled = False
    try:
        if ext in (".parquet", ".pq"):
            df = pd.read_parquet(path)
            n_rows = len(df)
            if n_rows > _TABLE_SUMMARY_ROWS:
                df = df.head(_TABLE_SUMMARY_ROWS); sampled = True
        elif ext in (".jsonl", ".ndjson"):
            n_rows = _count_lines(path)
            df = pd.read_json(path, lines=True, nrows=_TABLE_SUMMARY_ROWS)
            sampled = n_rows > len(df)
        else:  # csv / tsv / tab
            sep = "\t" if ext in (".tsv", ".tab") else ","
            # data rows ≈ lines minus header (best-effort; not exact with quoted newlines)
            n_rows = max(0, _count_lines(path) - 1)
            df = pd.read_csv(path, sep=sep, nrows=_TABLE_SUMMARY_ROWS)
            sampled = n_rows > len(df)
    except Exception as e:
        return f"Table read error ({path.name}): {e}", False

    n_cols = df.shape[1]
    columns = {str(c): str(t) for c, t in df.dtypes.items()}
    summary: dict = {
        "format": f"table ({ext.lstrip('.') or 'csv'})",
        "file": path.name,
        "shape": [int(n_rows), int(n_cols)],
        "columns": dict(list(columns.items())[:100]),
        "head": df.head(head).to_dict(orient="records"),
    }
    num = df.select_dtypes("number")
    if not num.empty:
        desc = num.describe().T[["mean", "std", "min", "50%", "max"]]
        summary["numeric_summary"] = {
            str(k): {kk: (None if pd.isna(vv) else round(float(vv), 4)) for kk, vv in v.items()}
            for k, v in desc.head(60).to_dict(orient="index").items()
        }
    na = df.isna().sum()
    na = na[na > 0].sort_values(ascending=False)
    if len(na):
        summary["columns_with_missing"] = {str(k): int(v) for k, v in list(na.items())[:20]}
    if sampled:
        summary["note"] = (
            f"shape.rows is the full count; dtypes / summary computed on the first "
            f"{_TABLE_SUMMARY_ROWS:,} rows. Use bash + pandas for full-data statistics."
        )
    return json.dumps(summary, indent=2, default=str), True


# ---------------------------------------------------------------------------
# rdkit_describe
# ---------------------------------------------------------------------------

def _rdkit_describe(smiles: str) -> tuple[str, bool]:
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem, Crippen, Descriptors, Lipinski, QED, rdMolDescriptors
    except ImportError:
        return "RDKit is not installed. Run: pip install rdkit", False
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return f"Invalid SMILES: {smiles!r}", False
    AllChem.Compute2DCoords(mol)
    mw = Descriptors.MolWt(mol)
    logp = Crippen.MolLogP(mol)
    tpsa = Descriptors.TPSA(mol)
    hbd = Lipinski.NumHDonors(mol)
    hba = Lipinski.NumHAcceptors(mol)
    rot = Lipinski.NumRotatableBonds(mol)
    rings = rdMolDescriptors.CalcNumRings(mol)
    aromatic = rdMolDescriptors.CalcNumAromaticRings(mol)
    heavy = mol.GetNumHeavyAtoms()
    formula = rdMolDescriptors.CalcMolFormula(mol)
    qed = QED.qed(mol)
    ro5 = sum([mw > 500, logp > 5, hbd > 5, hba > 10])
    return json.dumps({
        "input_smiles": smiles,
        "canonical_smiles": Chem.MolToSmiles(mol),
        "formula": formula,
        "molecular_weight": round(mw, 3),
        "logP": round(logp, 3),
        "tpsa": round(tpsa, 3),
        "h_bond_donors": hbd,
        "h_bond_acceptors": hba,
        "rotatable_bonds": rot,
        "num_rings": rings,
        "num_aromatic_rings": aromatic,
        "heavy_atoms": heavy,
        "qed": round(qed, 3),
        "lipinski_violations": ro5,
    }, indent=2), True


# ---------------------------------------------------------------------------
# UniProt
# ---------------------------------------------------------------------------

def _uniprot_lookup(accession: str = None, query: str = None, limit: int = 5) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    if not accession and not query:
        return "Provide either accession or query.", False
    limit = max(1, min(int(limit or 5), 25))

    if accession:
        url = f"https://rest.uniprot.org/uniprotkb/{accession}.json"
        try:
            r = _http_get(url)
        except Exception as e:
            return f"UniProt request failed: {e}", False
        if r.status_code == 404:
            return f"UniProt accession not found: {accession}", False
        if r.status_code != 200:
            return f"UniProt error {r.status_code}: {r.text[:200]}", False
        return _format_uniprot_entry(r.json()), True

    url = "https://rest.uniprot.org/uniprotkb/search"
    params = {"query": query, "format": "json", "size": limit, "fields":
              "accession,id,protein_name,organism_name,length,reviewed,xref_pdb"}
    try:
        r = _http_get(url, params=params)
    except Exception as e:
        return f"UniProt request failed: {e}", False
    if r.status_code != 200:
        return f"UniProt error {r.status_code}: {r.text[:200]}", False
    data = r.json()
    results = data.get("results", [])
    if not results:
        return json.dumps({"query": query, "matches": 0, "hits": []}, indent=2), True
    hits = []
    for entry in results:
        hits.append({
            "accession": entry.get("primaryAccession"),
            "id": entry.get("uniProtkbId"),
            "name": (entry.get("proteinDescription", {})
                     .get("recommendedName", {})
                     .get("fullName", {})
                     .get("value")),
            "organism": entry.get("organism", {}).get("scientificName"),
            "length": entry.get("sequence", {}).get("length"),
            "reviewed": entry.get("entryType", "").endswith("(Swiss-Prot)"),
        })
    return json.dumps({"query": query, "matches": len(results), "hits": hits}, indent=2), True


def _format_uniprot_entry(entry: dict) -> str:
    seq = entry.get("sequence", {}).get("value", "")
    rec = (entry.get("proteinDescription", {})
                .get("recommendedName", {})
                .get("fullName", {})
                .get("value"))
    fn_texts: list[str] = []
    for c in entry.get("comments", []):
        if c.get("commentType") == "FUNCTION":
            for t in c.get("texts", []):
                if t.get("value"):
                    fn_texts.append(t["value"])
    go_terms = []
    pdb_ids = []
    for x in entry.get("uniProtKBCrossReferences", []):
        db = x.get("database")
        if db == "GO":
            term = next((p["value"] for p in x.get("properties", [])
                        if p.get("key") == "GoTerm"), None)
            if term:
                go_terms.append(term)
        elif db == "PDB":
            pdb_ids.append(x.get("id"))
    return json.dumps({
        "accession": entry.get("primaryAccession"),
        "id": entry.get("uniProtkbId"),
        "name": rec,
        "organism": entry.get("organism", {}).get("scientificName"),
        "length": entry.get("sequence", {}).get("length"),
        "sequence_head": seq[:200],
        "function": " ".join(fn_texts)[:1500] or None,
        "go_terms": go_terms[:25],
        "pdb_ids": pdb_ids[:25],
        "reviewed": entry.get("entryType", "").endswith("(Swiss-Prot)"),
    }, indent=2)


# ---------------------------------------------------------------------------
# PubChem
# ---------------------------------------------------------------------------

def _pubchem_lookup(name: str = None, cid: int = None, smiles: str = None) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    if not (name or cid or smiles):
        return "Provide name, cid, or smiles.", False
    if cid is not None:
        ident = ("cid", str(cid))
    elif name:
        ident = ("name", name)
    else:
        ident = ("smiles", smiles)
    base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound"
    props = ("MolecularFormula,MolecularWeight,CanonicalSMILES,IsomericSMILES,InChI,"
             "InChIKey,IUPACName,XLogP,HBondDonorCount,HBondAcceptorCount,TPSA,"
             "RotatableBondCount,HeavyAtomCount")
    url = f"{base}/{ident[0]}/{_requests.utils.quote(ident[1])}/property/{props}/JSON"
    try:
        r = _http_get(url)
    except Exception as e:
        return f"PubChem request failed: {e}", False
    if r.status_code == 404:
        return f"PubChem: no match for {ident[0]}={ident[1]!r}", False
    if r.status_code != 200:
        return f"PubChem error {r.status_code}: {r.text[:200]}", False
    rows = r.json().get("PropertyTable", {}).get("Properties", [])
    if not rows:
        return f"PubChem: no properties returned for {ident[0]}={ident[1]!r}", False
    return json.dumps({"query": {ident[0]: ident[1]}, "matches": len(rows), "compounds": rows[:10]}, indent=2), True


# ---------------------------------------------------------------------------
# ChEMBL
# ---------------------------------------------------------------------------

def _chembl_lookup(chembl_id: str = None, name: str = None, limit: int = 5) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    if not (chembl_id or name):
        return "Provide chembl_id or name.", False
    base = "https://www.ebi.ac.uk/chembl/api/data/molecule"
    if chembl_id:
        url = f"{base}/{chembl_id}.json"
        try:
            r = _http_get(url)
        except Exception as e:
            return f"ChEMBL request failed: {e}", False
        if r.status_code == 404:
            return f"ChEMBL ID not found: {chembl_id}", False
        if r.status_code != 200:
            return f"ChEMBL error {r.status_code}: {r.text[:200]}", False
        return json.dumps(_summarise_chembl(r.json()), indent=2), True

    limit = max(1, min(int(limit or 5), 25))
    url = f"{base}/search.json"
    try:
        r = _http_get(url, params={"q": name, "limit": limit})
    except Exception as e:
        return f"ChEMBL request failed: {e}", False
    if r.status_code != 200:
        return f"ChEMBL error {r.status_code}: {r.text[:200]}", False
    data = r.json()
    mols = data.get("molecules", [])
    return json.dumps({
        "query": name,
        "matches": len(mols),
        "hits": [_summarise_chembl(m) for m in mols],
    }, indent=2), True


def _summarise_chembl(m: dict) -> dict:
    props = m.get("molecule_properties") or {}
    structs = m.get("molecule_structures") or {}
    return {
        "chembl_id": m.get("molecule_chembl_id"),
        "pref_name": m.get("pref_name"),
        "molecule_type": m.get("molecule_type"),
        "max_phase": m.get("max_phase"),
        "first_approval": m.get("first_approval"),
        "indication_class": m.get("indication_class"),
        "canonical_smiles": structs.get("canonical_smiles"),
        "inchi_key": structs.get("standard_inchi_key"),
        "molecular_weight": props.get("full_mwt"),
        "alogp": props.get("alogp"),
        "ro5_violations": props.get("num_ro5_violations"),
        "psa": props.get("psa"),
        "hba": props.get("hba"),
        "hbd": props.get("hbd"),
    }


# ---------------------------------------------------------------------------
# RCSB PDB
# ---------------------------------------------------------------------------

def _pdb_fetch(pdb_id: str, working_dir: str, format: str = "pdb",
               output_path: str = None) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    pdb_id = (pdb_id or "").strip().lower()
    if len(pdb_id) != 4:
        return "pdb_id must be a 4-character RCSB identifier.", False
    fmt = (format or "pdb").lower()
    if fmt not in ("pdb", "cif"):
        return "format must be 'pdb' or 'cif'.", False

    url = f"https://files.rcsb.org/download/{pdb_id}.{fmt}"
    try:
        r = _http_get(url, accept="*/*", timeout=60)
    except Exception as e:
        return f"RCSB request failed: {e}", False
    if r.status_code != 200:
        return f"RCSB error {r.status_code} for {pdb_id}.{fmt}", False

    out = output_path or f"{pdb_id}.{fmt}"
    out_resolved = _resolve(out, working_dir)
    out_resolved.parent.mkdir(parents=True, exist_ok=True)
    out_resolved.write_bytes(r.content)

    # short header summary
    head = r.text[:4000] if fmt == "pdb" else r.text[:2000]
    title = ""
    chains: set[str] = set()
    resolution = None
    for line in head.splitlines():
        if line.startswith("TITLE"):
            title += " " + line[10:80].strip()
        elif line.startswith("ATOM") and len(line) > 21:
            chains.add(line[21])
        elif line.startswith("REMARK   2 RESOLUTION."):
            try:
                resolution = float(line.split()[3])
            except (ValueError, IndexError):
                pass
    return json.dumps({
        "pdb_id": pdb_id.upper(),
        "format": fmt,
        "saved_to": str(out_resolved),
        "size_bytes": len(r.content),
        "title": title.strip()[:200] or None,
        "chains_seen_in_head": sorted(chains),
        "resolution_A": resolution,
    }, indent=2), True


# ---------------------------------------------------------------------------
# AlphaFold DB
# ---------------------------------------------------------------------------

def _alphafold_fetch(uniprot_id: str, working_dir: str,
                     output_path: str = None) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    uniprot_id = (uniprot_id or "").strip().upper()
    if not uniprot_id:
        return "uniprot_id is required.", False
    meta_url = f"https://alphafold.ebi.ac.uk/api/prediction/{uniprot_id}"
    try:
        meta = _http_get(meta_url)
    except Exception as e:
        return f"AlphaFold request failed: {e}", False
    if meta.status_code == 404:
        return f"AlphaFold: no prediction for {uniprot_id}", False
    if meta.status_code != 200:
        return f"AlphaFold error {meta.status_code}: {meta.text[:200]}", False
    data = meta.json()
    if not data:
        return f"AlphaFold returned empty result for {uniprot_id}", False
    entry = data[0]
    pdb_url = entry.get("pdbUrl")
    if not pdb_url:
        return f"AlphaFold entry has no pdbUrl for {uniprot_id}", False
    try:
        r = _http_get(pdb_url, accept="*/*", timeout=60)
    except Exception as e:
        return f"AlphaFold PDB download failed: {e}", False
    if r.status_code != 200:
        return f"AlphaFold PDB error {r.status_code}", False

    out = output_path or f"AF-{uniprot_id}.pdb"
    out_resolved = _resolve(out, working_dir)
    out_resolved.parent.mkdir(parents=True, exist_ok=True)
    out_resolved.write_bytes(r.content)

    # mean pLDDT lives in column 60-66 of ATOM records (B-factor)
    plddts: list[float] = []
    for line in r.text.splitlines():
        if line.startswith("ATOM") and len(line) >= 66:
            try:
                plddts.append(float(line[60:66]))
            except ValueError:
                pass
    mean_plddt = sum(plddts) / len(plddts) if plddts else None
    return json.dumps({
        "uniprot_id": uniprot_id,
        "model_version": entry.get("latestVersion"),
        "uniprot_sequence_length": entry.get("uniprotEnd"),
        "saved_to": str(out_resolved),
        "size_bytes": len(r.content),
        "mean_plddt": round(mean_plddt, 2) if mean_plddt is not None else None,
        "model_url": pdb_url,
    }, indent=2), True


# ---------------------------------------------------------------------------
# NCBI GEO via E-utilities
# ---------------------------------------------------------------------------

def _geo_search(accession: str = None, query: str = None, limit: int = 5) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    if not (accession or query):
        return "Provide accession or query.", False
    limit = max(1, min(int(limit or 5), 25))
    eutils = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    if accession:
        term = f"{accession}[ACCN]"
    else:
        term = query
    try:
        es = _http_get(f"{eutils}/esearch.fcgi",
                       params={"db": "gds", "term": term, "retmax": limit, "retmode": "json"})
    except Exception as e:
        return f"NCBI esearch failed: {e}", False
    if es.status_code != 200:
        return f"NCBI esearch error {es.status_code}: {es.text[:200]}", False
    ids = es.json().get("esearchresult", {}).get("idlist", []) or []
    if not ids:
        return json.dumps({"query": term, "matches": 0, "hits": []}, indent=2), True
    try:
        sm = _http_get(f"{eutils}/esummary.fcgi",
                       params={"db": "gds", "id": ",".join(ids), "retmode": "json"})
    except Exception as e:
        return f"NCBI esummary failed: {e}", False
    if sm.status_code != 200:
        return f"NCBI esummary error {sm.status_code}: {sm.text[:200]}", False
    res = sm.json().get("result", {})
    hits = []
    for uid in res.get("uids", []):
        e = res.get(uid, {})
        hits.append({
            "accession": e.get("accession"),
            "title": e.get("title"),
            "summary": (e.get("summary") or "")[:400],
            "organism": e.get("taxon"),
            "platform": e.get("gpl"),
            "n_samples": e.get("n_samples"),
            "type": e.get("gdstype"),
            "pubmed_ids": e.get("pubmedids"),
        })
    return json.dumps({"query": term, "matches": len(hits), "hits": hits}, indent=2), True


# ---------------------------------------------------------------------------
# ENA file report
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# pdf_ocr — render PDF pages and OCR them
# ---------------------------------------------------------------------------

def _parse_page_spec(spec: str, total: int) -> list[int]:
    """Parse '1', '1-5', '1,3,5', 'all' into a 0-indexed list of page numbers."""
    spec = (spec or "all").strip().lower()
    if spec == "all":
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a_i, b_i = int(a), int(b)
            for n in range(a_i, b_i + 1):
                if 1 <= n <= total:
                    out.append(n - 1)
        else:
            n = int(part)
            if 1 <= n <= total:
                out.append(n - 1)
    # de-dup preserving order
    seen: set[int] = set()
    deduped: list[int] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _pdf_ocr(path: str, working_dir: str, pages: str = "all", dpi: int = 200,
             output_dir: str = None, lang: str = "eng") -> tuple[str, bool]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ("PyMuPDF is not installed. Run: pip install pymupdf "
                "(needed to render PDF pages to images).", False)
    try:
        import pytesseract  # type: ignore
        _has_tess = True
    except ImportError:
        pytesseract = None
        _has_tess = False
    try:
        from PIL import Image
    except ImportError:
        return ("Pillow is not installed. Run: pip install pillow "
                "(needed for image handling).", False)

    resolved = _resolve(path, working_dir)
    if not resolved.exists():
        return f"File not found: {resolved}", False
    if resolved.suffix.lower() != ".pdf":
        return f"Not a PDF: {resolved.name}", False

    save_dir: Path | None = None
    if output_dir:
        save_dir = _resolve(output_dir, working_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(str(resolved))
    except Exception as e:
        return f"Could not open PDF: {e}", False

    total = doc.page_count
    page_indices = _parse_page_spec(pages, total)
    if not page_indices:
        doc.close()
        return f"No valid pages selected from spec={pages!r} (PDF has {total} pages).", False

    # Hard cap so a model doesn't accidentally OCR a 500-page document
    cap = 25
    capped = False
    if len(page_indices) > cap:
        page_indices = page_indices[:cap]
        capped = True

    zoom = max(1.0, dpi / 72.0)
    matrix = fitz.Matrix(zoom, zoom)

    page_results: list[dict] = []
    tess_warning: str | None = None

    for p_idx in page_indices:
        page = doc.load_page(p_idx)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        png_bytes = pix.tobytes("png")
        saved_path: str | None = None
        if save_dir is not None:
            fname = f"page_{p_idx+1:04d}.png"
            target = save_dir / fname
            target.write_bytes(png_bytes)
            saved_path = str(target)

        ocr_text: str | None = None
        if _has_tess:
            try:
                img = Image.open(io.BytesIO(png_bytes))
                ocr_text = pytesseract.image_to_string(img, lang=lang)
            except pytesseract.TesseractNotFoundError:
                _has_tess = False
                tess_warning = (
                    "tesseract binary not found on PATH. Install: "
                    "macOS `brew install tesseract`, Debian `apt install tesseract-ocr`. "
                    "Page images still saved if output_dir was provided."
                )
            except Exception as e:
                ocr_text = f"[OCR error on page {p_idx+1}: {e}]"

        # truncate per-page text so 25 pages don't blow the context window
        if ocr_text and len(ocr_text) > 4000:
            ocr_text = ocr_text[:4000] + "\n...[truncated, page text > 4000 chars]"

        page_results.append({
            "page": p_idx + 1,
            "saved_image": saved_path,
            "ocr_text": ocr_text,
        })

    doc.close()

    return json.dumps({
        "file": resolved.name,
        "total_pages": total,
        "pages_processed": len(page_results),
        "capped_at": cap if capped else None,
        "dpi": dpi,
        "lang": lang,
        "ocr_available": _has_tess,
        "tesseract_warning": tess_warning,
        "pages": page_results,
    }, indent=2), True


_ENA_FIELDS = (
    "run_accession,study_accession,sample_accession,experiment_accession,"
    "instrument_platform,instrument_model,library_layout,library_strategy,"
    "library_source,library_selection,read_count,base_count,fastq_ftp,"
    "fastq_md5,fastq_bytes,sample_title,scientific_name,tax_id"
)


def _ena_fetch(accession: str, result: str = "read_run") -> tuple[str, bool]:
    if err := _need_requests():
        return err
    accession = (accession or "").strip()
    if not accession:
        return "accession is required.", False
    if result not in ("read_run", "study", "sample", "experiment"):
        return "result must be one of: read_run, study, sample, experiment.", False
    url = "https://www.ebi.ac.uk/ena/portal/api/filereport"
    params = {
        "accession": accession,
        "result": result,
        "fields": _ENA_FIELDS if result == "read_run" else None,
        "format": "json",
        "limit": 100,
    }
    params = {k: v for k, v in params.items() if v is not None}
    try:
        r = _http_get(url, params=params)
    except Exception as e:
        return f"ENA request failed: {e}", False
    if r.status_code != 200:
        return f"ENA error {r.status_code}: {r.text[:200]}", False
    try:
        rows = r.json()
    except Exception:
        return f"ENA returned non-JSON: {r.text[:200]}", False
    if not rows:
        return f"ENA: no records for {accession}", False
    # split fastq_ftp / fastq_md5 / fastq_bytes ; -> lists for clarity
    for row in rows[:50]:
        for k in ("fastq_ftp", "fastq_md5", "fastq_bytes"):
            v = row.get(k)
            if isinstance(v, str) and ";" in v:
                row[k] = v.split(";")
    return json.dumps({
        "accession": accession,
        "result_type": result,
        "matches": len(rows),
        "records": rows[:50],
    }, indent=2, default=str), True
