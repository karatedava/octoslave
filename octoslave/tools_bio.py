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
                "Schema-aware preview of a biological / chemical data file. "
                "Auto-detects format (FASTA, FASTQ, VCF, GFF/GTF, PDB, mmCIF, "
                "Matrix Market .mtx, AnnData .h5ad, SMILES .smi/.smiles, SDF) "
                "and returns counts, schema, and a small head preview. "
                "ALWAYS use this on bio/chem files instead of read_file — "
                "read_file will dump millions of lines for large datasets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the data file"},
                    "format": {
                        "type": "string",
                        "description": (
                            "Optional override. One of: fasta, fastq, vcf, gff, gtf, "
                            "pdb, cif, mtx, h5ad, smi, sdf. Auto-detected from "
                            "extension if omitted."
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
    {
        "type": "function",
        "function": {
            "name": "kegg_lookup",
            "description": (
                "Query the KEGG database (free REST API, no auth required). "
                "Operations: (1) 'find' — search for compounds/reactions/enzymes/pathways by keyword "
                "or EC number; (2) 'get' — retrieve a KEGG entry by ID (e.g. R04038 reaction, "
                "C15519 compound, ec:1.14.14.1 enzyme class, path:map00900 pathway); "
                "(3) 'link' — cross-references between databases (e.g. reactions involving a compound). "
                "KEGG ID prefixes: C###### = compound, R###### = reaction, ec:X.X.X.X = enzyme, "
                "path:map##### = pathway. "
                "USE THIS INSTEAD of RetroBioCat (returns 404) or manual web search for biocatalytic "
                "pathway data. Essential for: EC number → enzyme name, compound → reactions, "
                "pathway enumeration (terpenoid: path:map00900, path:map01060)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "description": "One of: 'find' (search), 'get' (entry details by ID), 'link' (cross-refs)",
                    },
                    "database": {
                        "type": "string",
                        "description": (
                            "For 'find': one of compound, reaction, enzyme, pathway, glycan, drug "
                            "(default: compound)"
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": "For 'find': search term or EC number (e.g. 'terpenoid hydroxylation', '1.14.14.1')",
                    },
                    "entry_id": {
                        "type": "string",
                        "description": (
                            "For 'get' or 'link': KEGG ID (e.g. 'R04038', 'C15519', "
                            "'ec:1.14.14.1', 'path:map00900')"
                        ),
                    },
                    "link_target": {
                        "type": "string",
                        "description": "For 'link': target database (e.g. 'reaction', 'enzyme', 'compound', 'pathway')",
                    },
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rdkit_admet",
            "description": (
                "Comprehensive ADMET property prediction using RDKit. Returns: Lipinski Ro5, Veber, "
                "Egan Egg, Ghose filters; BBB penetration estimate; ESOL aqueous solubility; "
                "hERG blocking alert; Ames mutagenicity structural alerts (nitro, epoxide, Michael "
                "acceptor, aromatic amine, alkyl halide); CYP substrate likelihood; and "
                "enzyme_substrate_class for biocatalytic context. "
                "FOR BIOCATALYTIC RETROSYNTHESIS: use enzyme_substrate_class, hba, tpsa, and "
                "mutagenicity_alerts as primary outputs — NOT Lipinski/Veber/QED which are "
                "irrelevant for non-drug terpenoid substrates. "
                "Use INSTEAD of rdkit_describe when ADMET or biocatalytic substrate context matters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {"type": "string", "description": "SMILES string"},
                    "context": {
                        "type": "string",
                        "description": (
                            "'drug' (standard ADMET for oral bioavailability) or "
                            "'enzyme_substrate' (biocatalysis focus — ignores drug-likeness filters). "
                            "Default: 'drug'"
                        ),
                    },
                },
                "required": ["smiles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enzyme_cost_lookup",
            "description": (
                "Look up enzyme kit prices from a verified static catalog "
                "(Sigma-Aldrich, Merck, Prozomix, Novozymes). Returns supplier, SKU, price range, "
                "and product URL. "
                "USE THIS INSTEAD of web scraping supplier pages — Prozomix and Sigma pages are "
                "JS-rendered and always return $0.00 or empty data when scraped with requests/bs4. "
                "If the enzyme is not in the static table, returns not_found with search suggestions. "
                "Query by enzyme name, EC number, common name, or UniProt ID "
                "(e.g. 'CYP102A1', 'P450 BM3', '1.14.14.1', 'alcohol dehydrogenase', 'P14779')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Enzyme name, EC number, common name, or UniProt accession",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pains_alerts",
            "description": (
                "Screen a SMILES string for pan-assay interference compounds (PAINS) "
                "and Brenk/NIH structural alerts using RDKit FilterCatalog. "
                "PAINS are substructures that frequently give false positives in HTS assays "
                "(colloidal aggregators, redox cyclers, thiol reactive groups, etc.). "
                "Brenk flags metabolically unstable or potentially toxic groups. "
                "Use this BEFORE shortlisting candidates — PAINS hits need scaffold redesign "
                "before biological testing. Returns alert name, filter type, and recommendation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {"type": "string", "description": "SMILES string to screen"},
                },
                "required": ["smiles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rdkit_scaffold",
            "description": (
                "Extract the Bemis-Murcko scaffold of a molecule and generate scaffold-hop "
                "candidates via common bioisosteric replacements. "
                "Returns: scaffold SMILES, generic scaffold, ring system count, framework type "
                "(aromatic/mixed/saturated), and a list of bioisostere suggestions for substructures "
                "detected in the molecule (phenyl→pyridyl, COOH→tetrazole, amide→triazole, Cl→F, etc.). "
                "Optionally accepts a reference SMILES to compute MCS and highlight structural divergence "
                "between query and reference (useful for scaffold-hop analysis). "
                "Use when the current scaffold has ADMET liabilities or PAINS alerts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {"type": "string", "description": "Query molecule SMILES"},
                    "reference_smiles": {
                        "type": "string",
                        "description": (
                            "Optional reference molecule SMILES. If provided, MCS is computed "
                            "and structural divergence is highlighted for bioisostere identification."
                        ),
                    },
                },
                "required": ["smiles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swissadme_fetch",
            "description": (
                "Predict ADMET and physicochemical properties by querying SwissADME "
                "(swissadme.ch). Returns: consensus LogP, water solubility (ESOL/Ali), "
                "GI absorption, BBB permeability, P-gp substrate status, CYP1A2/2C19/2C9/2D6/3A4 "
                "inhibition, Lipinski/Ghose/Veber/Egan/Muegge druglikeness, PAINS alerts, "
                "Brenk alerts, leadlikeness, and synthetic accessibility score. "
                "Requires internet. Falls back gracefully with rdkit_admet recommendation "
                "if the server is unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {"type": "string", "description": "SMILES string to evaluate"},
                },
                "required": ["smiles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pkcsm_fetch",
            "description": (
                "Predict pharmacokinetic/ADMET properties via the pkCSM server "
                "(biosig.unimelb.edu.au/pkcsm). Returns numerical ML predictions for: "
                "absorption (Caco-2, intestinal absorption %, P-gp substrate/inhibitor, "
                "skin permeability), distribution (VDss, fraction unbound, BBB, CNS permeability), "
                "metabolism (CYP1A2/2C19/2C9/2D6/3A4 substrate and inhibitor), "
                "excretion (total clearance, renal OCT2 substrate), "
                "toxicity (AMES mutagenicity, hERG, max tolerated dose, hepatotoxicity, "
                "skin sensitisation, T. pyriformis, minnow toxicity). "
                "Returns numeric values, not just categories. Requires internet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {"type": "string", "description": "SMILES string"},
                    "endpoint": {
                        "type": "string",
                        "description": (
                            "Endpoint group: 'all' (default), 'absorption', 'distribution', "
                            "'metabolism', 'excretion', 'toxicity'."
                        ),
                    },
                },
                "required": ["smiles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "surechembl_search",
            "description": (
                "Search the SureChEMBL patent chemistry database (surechembl.org) for patents "
                "containing a query compound (by SMILES similarity/substructure) or text. "
                "Returns: patent IDs, titles, publication dates, assignees, and patent URLs. "
                "SureChEMBL indexes >20M documents from USPTO, EPO, WIPO, and JPO. "
                "Use to assess IP landscape before advancing a scaffold — if >3 patents cover "
                "the scaffold class, flag for FTO (freedom-to-operate) analysis. "
                "Requires internet. Falls back to web_search instructions if unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {
                        "type": "string",
                        "description": "SMILES for similarity/substructure patent search",
                    },
                    "query": {
                        "type": "string",
                        "description": "Free-text search (compound name, scaffold class, target name)",
                    },
                    "similarity": {
                        "type": "number",
                        "description": "Tanimoto similarity cutoff for SMILES search (0.0–1.0, default 0.85)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max patent results (default 10, max 50)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vina_dock",
            "description": (
                "Run molecular docking using AutoDock Vina (or Smina/Gnina if Vina is absent) "
                "to score binding of a ligand SMILES to a target protein. "
                "Receptor can be a 4-letter PDB ID (auto-downloaded) or a local .pdb/.pdbqt path. "
                "Requires: vina/smina/gnina on PATH + obabel for PDBQT conversion. "
                "Returns top binding poses with affinity (kcal/mol) and RMSD values. "
                "Typical ranges: ≤ −9 kcal/mol = very potent, −7 to −9 = moderate, > −5 = weak. "
                "Use to replace 2D-similarity proxy scores with real docking energies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ligand_smiles": {"type": "string", "description": "Ligand SMILES string"},
                    "receptor_source": {
                        "type": "string",
                        "description": "4-letter PDB ID (e.g. '1ATP') or local path to .pdb/.pdbqt",
                    },
                    "pocket_x": {"type": "number", "description": "Pocket centre X coordinate (Å)"},
                    "pocket_y": {"type": "number", "description": "Pocket centre Y coordinate (Å)"},
                    "pocket_z": {"type": "number", "description": "Pocket centre Z coordinate (Å)"},
                    "box_size_x": {"type": "number", "description": "Search box X size in Å (default 20)"},
                    "box_size_y": {"type": "number", "description": "Search box Y size in Å (default 20)"},
                    "box_size_z": {"type": "number", "description": "Search box Z size in Å (default 20)"},
                    "exhaustiveness": {
                        "type": "integer",
                        "description": "Search exhaustiveness 1–32 (default 8; increase for accuracy)",
                    },
                    "n_poses": {
                        "type": "integer",
                        "description": "Number of top binding poses to return (default 5)",
                    },
                },
                "required": ["ligand_smiles", "receptor_source", "pocket_x", "pocket_y", "pocket_z"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bindingdb_lookup",
            "description": (
                "Query BindingDB (bindingdb.org) for experimentally measured binding affinities. "
                "Search by compound SMILES (similarity ≥ 85% Tanimoto) or target gene symbol. "
                "Returns: IC50/Ki/Kd/EC50 in nM, assay type, target name, and literature reference. "
                "Essential for selectivity profiling — e.g. find all targets a compound hits "
                "at < 100 nM to identify off-target risks (kinase family cross-reactivity, "
                "CYP inhibition, hERG hits) before committing to in vitro studies. "
                "Falls back to ChEMBL/web_search instructions if unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "smiles": {
                        "type": "string",
                        "description": "Query compound SMILES (similarity search, Tanimoto ≥ 85%)",
                    },
                    "target_gene": {
                        "type": "string",
                        "description": "Target gene symbol (e.g. 'EGFR', 'CDK2', 'CYP3A4') to find all binders",
                    },
                    "ki_cutoff_nm": {
                        "type": "number",
                        "description": "Return only affinities ≤ this value in nM (default 10000 = all)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 50)",
                    },
                },
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
        if name == "kegg_lookup":
            return _kegg_lookup(**args)
        if name == "rdkit_admet":
            return _rdkit_admet(**args)
        if name == "enzyme_cost_lookup":
            return _enzyme_cost_lookup(**args)
        if name == "pains_alerts":
            return _pains_alerts(**args)
        if name == "rdkit_scaffold":
            return _rdkit_scaffold(**args)
        if name == "swissadme_fetch":
            return _swissadme_fetch(**args)
        if name == "pkcsm_fetch":
            return _pkcsm_fetch(**args)
        if name == "surechembl_search":
            return _surechembl_search(**args)
        if name == "vina_dock":
            return _vina_dock(working_dir=working_dir, **args)
        if name == "bindingdb_lookup":
            return _bindingdb_lookup(**args)
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


# ---------------------------------------------------------------------------
# KEGG REST API
# ---------------------------------------------------------------------------

def _kegg_lookup(
    operation: str,
    database: str = "compound",
    query: str = None,
    entry_id: str = None,
    link_target: str = None,
) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    base = "https://rest.kegg.jp"
    operation = (operation or "").strip().lower()

    if operation == "find":
        if not query:
            return "query is required for 'find' operation.", False
        db = (database or "compound").strip().lower()
        valid_dbs = {"compound", "reaction", "enzyme", "pathway", "glycan", "drug", "ko", "module"}
        if db not in valid_dbs:
            return f"database must be one of: {', '.join(sorted(valid_dbs))}", False
        url = f"{base}/find/{db}/{_requests.utils.quote(str(query))}"
        try:
            r = _http_get(url, accept="text/plain")
        except Exception as e:
            return f"KEGG request failed: {e}", False
        if r.status_code == 404:
            return json.dumps({"operation": "find", "database": db, "query": query, "results": [], "note": "No matches found."}, indent=2), True
        if r.status_code != 200:
            return f"KEGG error {r.status_code}: {r.text[:200]}", False
        results = []
        for line in r.text.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            results.append({"id": parts[0], "description": parts[1] if len(parts) > 1 else ""})
        return json.dumps({"operation": "find", "database": db, "query": query, "results": results[:30]}, indent=2), True

    elif operation == "get":
        if not entry_id:
            return "entry_id is required for 'get' operation.", False
        url = f"{base}/get/{entry_id}"
        try:
            r = _http_get(url, accept="text/plain")
        except Exception as e:
            return f"KEGG request failed: {e}", False
        if r.status_code == 404:
            return f"KEGG entry not found: {entry_id}", False
        if r.status_code != 200:
            return f"KEGG error {r.status_code}: {r.text[:200]}", False
        return json.dumps({"operation": "get", "entry_id": entry_id, "record": _parse_kegg_flat(r.text)}, indent=2), True

    elif operation == "link":
        if not entry_id or not link_target:
            return "entry_id and link_target are required for 'link' operation.", False
        url = f"{base}/link/{link_target}/{entry_id}"
        try:
            r = _http_get(url, accept="text/plain")
        except Exception as e:
            return f"KEGG request failed: {e}", False
        if r.status_code == 404:
            return json.dumps({"operation": "link", "source": entry_id, "target_db": link_target, "links": [], "note": "No links found."}, indent=2), True
        if r.status_code != 200:
            return f"KEGG error {r.status_code}: {r.text[:200]}", False
        links = []
        for line in r.text.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) == 2:
                links.append({"source": parts[0], "target": parts[1]})
        return json.dumps({"operation": "link", "source": entry_id, "target_db": link_target, "links": links[:50]}, indent=2), True

    else:
        return f"Unknown operation: {operation!r}. Use 'find', 'get', or 'link'.", False


def _parse_kegg_flat(text: str) -> dict:
    """Parse KEGG flat-file format into a dict. Stops at '///'."""
    record: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in text.split("\n"):
        if line.startswith("///"):
            break
        if not line:
            continue
        key_part = line[:12].rstrip()
        val_part = line[12:].strip()
        if key_part:
            current_key = key_part
            record.setdefault(current_key, [])
        if current_key and val_part:
            record[current_key].append(val_part)
    return {k: " | ".join(v) if len(v) > 1 else v[0] if v else "" for k, v in record.items()}


# ---------------------------------------------------------------------------
# rdkit_admet — comprehensive ADMET property prediction
# ---------------------------------------------------------------------------

def _rdkit_admet(smiles: str, context: str = "drug") -> tuple[str, bool]:
    try:
        from rdkit import Chem
        from rdkit.Chem import Crippen, Descriptors, Lipinski, QED, rdMolDescriptors
    except ImportError:
        return "RDKit is not installed. Run: pip install rdkit", False

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return f"Invalid SMILES: {smiles!r}", False

    mw    = Descriptors.MolWt(mol)
    logp  = Crippen.MolLogP(mol)
    tpsa  = Descriptors.TPSA(mol)
    hbd   = Lipinski.NumHDonors(mol)
    hba   = Lipinski.NumHAcceptors(mol)
    rot   = Lipinski.NumRotatableBonds(mol)
    rings = rdMolDescriptors.CalcNumRings(mol)
    arom  = rdMolDescriptors.CalcNumAromaticRings(mol)
    heavy = mol.GetNumHeavyAtoms()
    n_het = sum(1 for a in mol.GetAtoms() if a.GetAtomicNum() not in (6, 1, 0))

    # Bioavailability filters
    ro5  = (mw <= 500 and logp <= 5 and hbd <= 5 and hba <= 10)
    veb  = (rot <= 10 and tpsa <= 140)
    egan = (tpsa <= 131.6 and logp <= 5.88)
    gho  = (160 <= mw <= 480 and -0.4 <= logp <= 5.6 and 20 <= heavy <= 70)

    # BBB heuristic (CNS-MPO simplified)
    bbb = (tpsa < 90 and hbd <= 3 and mw < 450 and logp > 0)

    # ESOL solubility (Delaney 2004)
    esol = round(0.16 - 0.63 * logp - 0.0062 * mw + 0.066 * rot - 0.74 * arom, 2)
    sol_cls = ("high" if esol > -1 else "moderate" if esol > -3 else "low" if esol > -5 else "very_low")

    # hERG alert: basic non-aromatic N + logP > 2.5
    has_basic_n = any(
        a.GetAtomicNum() == 7 and not a.GetIsAromatic() and a.GetTotalNumHs() > 0
        for a in mol.GetAtoms()
    )
    herg = has_basic_n and logp > 2.5

    # Mutagenicity structural alerts (Ames-relevant SMARTS)
    _ALERT_SMARTS = {
        "nitro_group":      "[N+](=O)[O-]",
        "aromatic_amine":   "c-[NH2]",
        "epoxide":          "[C;R1]1[O;R1][C;R1]1",
        "michael_acceptor": "[CX3](=O)[CX3]=[CX3]",
        "aldehyde":         "[CX3H1](=O)[#6]",
        "alkyl_halide":     "[CX4][F,Cl,Br,I]",
        "azo":              "[NX2]=[NX2]",
    }
    alerts = [name for name, s in _ALERT_SMARTS.items()
              if (p := Chem.MolFromSmarts(s)) and mol.HasSubstructMatch(p)]

    # CYP substrate likelihood heuristic
    cyp_likely = (250 <= mw <= 500 and 1 <= logp <= 5)

    # Enzymatic substrate classification (biocatalysis context)
    if mw < 350 and n_het <= 2 and rings >= 2 and arom == 0:
        sub_class = "terpenoid-like"
    elif rings >= 3 and n_het <= 3:
        sub_class = "complex-natural-product"
    elif ro5:
        sub_class = "drug-like"
    else:
        sub_class = "other"

    note = (
        "Biocatalysis context: use enzyme_substrate_class, hba, tpsa, and mutagenicity_alerts. "
        "Lipinski/Veber/Ghose are irrelevant for non-drug terpenoid substrates."
        if context == "enzyme_substrate" else
        "Standard ADMET profile. For biocatalytic retrosynthesis pass context='enzyme_substrate'."
    )

    return json.dumps({
        "smiles": smiles,
        "context": context,
        "molecular_weight": round(mw, 2),
        "logP": round(logp, 3),
        "tpsa": round(tpsa, 2),
        "hbd": hbd,
        "hba": hba,
        "rot_bonds": rot,
        "rings": rings,
        "aromatic_rings": arom,
        "heavy_atoms": heavy,
        "n_heteroatoms": n_het,
        "lipinski_ro5": ro5,
        "veber_oral": veb,
        "egan_egg": egan,
        "ghose": gho,
        "bbb_penetration_likely": bbb,
        "esol_logS": esol,
        "aqueous_solubility_class": sol_cls,
        "herg_alert": herg,
        "mutagenicity_alerts": alerts,
        "cyp_substrate_likely": cyp_likely,
        "enzyme_substrate_class": sub_class,
        "note": note,
    }, indent=2), True


# ---------------------------------------------------------------------------
# enzyme_cost_lookup — verified static price catalog
# ---------------------------------------------------------------------------

_ENZYME_CATALOG: list[dict] = [
    {
        "aliases": ["cyp102a1", "p450 bm3", "p450bm3", "cyp102", "1.14.14.1", "p14779",
                    "p450 bm-3", "bm3"],
        "name": "Cytochrome P450 BM3 (CYP102A1) — wild-type",
        "ec": "1.14.14.1",
        "uniprot": "P14779",
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "SML2222",
             "price": "~$195 / 0.5 mg",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/sml2222",
             "availability": "commercial"},
            {"name": "Prozomix", "product": "P450 BM3 Panel Kit (15 variants)",
             "price": "~£350–500 (MTA/license required — email info@prozomix.com)",
             "url": "https://www.prozomix.com/products/oxidation",
             "availability": "MTA required"},
        ],
        "note": (
            "Most characterised bacterial P450. Wild-type has low activity on large terpenoid "
            "substrates. F87V and A82F/F87V variants show improved terpenoid hydroxylation "
            "(Renata 2021, JACS). Requires NADPH-regeneration system (GDH/glucose ~$15/100 rxn)."
        ),
    },
    {
        "aliases": ["cyp101a1", "p450cam", "cyp101", "1.14.15.1", "p00183"],
        "name": "Cytochrome P450cam (CYP101A1)",
        "ec": "1.14.15.1",
        "uniprot": "P00183",
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "C3546",
             "price": "~$125 / 1 mg",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/c3546",
             "availability": "commercial"},
        ],
        "note": "Narrow substrate scope — primarily camphor. Poor activity on diterpene substrates.",
    },
    {
        "aliases": ["adh", "alcohol dehydrogenase", "1.1.1.1", "hladh", "horse liver adh",
                    "yeast adh", "yadh", "p00327"],
        "name": "Alcohol dehydrogenase (horse liver, HLADH; or yeast YADH)",
        "ec": "1.1.1.1",
        "uniprot": "P00327",
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "A3263",
             "price": "~$45 / 750 U (horse liver)",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/a3263",
             "availability": "commercial"},
            {"name": "Sigma-Aldrich (Merck)", "sku": "A7011",
             "price": "~$36 / 1000 U (yeast)",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/a7011",
             "availability": "commercial"},
        ],
        "note": "Cofactor: NAD+ (Sigma N7004 ~$25/g). Good for secondary alcohol oxidation/reduction.",
    },
    {
        "aliases": ["laccase", "1.10.3.2", "trametes versicolor laccase", "q12718"],
        "name": "Laccase (Trametes versicolor)",
        "ec": "1.10.3.2",
        "uniprot": "Q12718",
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "38429",
             "price": "~$55 / 1 KU",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/38429",
             "availability": "commercial"},
        ],
        "note": "Requires only O2. Active mainly on phenolic substrates; poor on aliphatic terpenoids.",
    },
    {
        "aliases": ["calb", "lipase b", "candida antarctica lipase b", "novozym 435",
                    "3.1.1.3", "p41365"],
        "name": "Lipase B — Candida antarctica (CALB / Novozym 435)",
        "ec": "3.1.1.3",
        "uniprot": "P41365",
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "L4777",
             "price": "~$160 / 1 g (immobilised)",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/l4777",
             "availability": "commercial"},
            {"name": "Novozymes / ImmChem", "product": "Novozym 435",
             "price": "~$450 / kg (bulk)",
             "url": "https://www.novozymes.com",
             "availability": "commercial"},
        ],
        "note": "Best for ester synthesis/hydrolysis; not for C-H hydroxylation.",
    },
    {
        "aliases": ["bvmo", "cyclohexanone monooxygenase", "chmo", "1.14.13.22",
                    "baeyer-villiger monooxygenase", "q9r2f5"],
        "name": "Baeyer–Villiger Monooxygenase (CHMO / BVMO)",
        "ec": "1.14.13.22",
        "uniprot": "Q9R2F5",
        "suppliers": [
            {"name": "Prozomix", "product": "BVMO Panel Kit (24 enzymes)",
             "price": "~£250–450 (contact for quote)",
             "url": "https://www.prozomix.com/products/oxidation",
             "availability": "commercial, quote required"},
        ],
        "note": (
            "NADPH-dependent. Converts ketones → lactones/esters. "
            "Requires regeneration system (GDH/glucose ~$15/100 rxn). "
            "Use for terpenone → lactone if the target scaffold allows ring expansion."
        ),
    },
    {
        "aliases": ["oye", "ene-reductase", "old yellow enzyme", "1.6.99.1",
                    "enoate reductase"],
        "name": "Ene-reductase / Old Yellow Enzyme (OYE family)",
        "ec": "1.6.99.1",
        "suppliers": [
            {"name": "Prozomix", "product": "Ene-reductase Panel Kit (36 enzymes)",
             "price": "~£350–550 (contact for quote)",
             "url": "https://www.prozomix.com/products/reduction",
             "availability": "commercial, quote required"},
        ],
        "note": "NADPH-dependent C=C reduction. High yields (40–90%) on activated alkenes.",
    },
    {
        "aliases": ["gdh", "glucose dehydrogenase", "1.1.1.47", "nadph regeneration",
                    "cofactor regeneration", "g4134"],
        "name": "Glucose Dehydrogenase (GDH) — NADPH regeneration",
        "ec": "1.1.1.47",
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "G4134",
             "price": "~$58 / 500 U",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/g4134",
             "availability": "commercial"},
        ],
        "note": (
            "Required cofactor regeneration partner for CYP, BVMO, OYE, and ADH reactions. "
            "Add glucose (~$0.01/g) + NADP+ (Sigma N0505 ~$42/100 mg). "
            "Effective cost: ~$0.002/µmol NADPH regenerated."
        ),
        "cofactors": ["NADP+ — Sigma N0505, ~$42/100 mg", "D-Glucose — Sigma G7021, ~$35/kg"],
    },
    {
        "aliases": ["nad+", "nadh", "nadp+", "nadph", "coenzyme", "cofactor",
                    "n7004", "n0505"],
        "name": "NAD+/NADH/NADP+/NADPH cofactors",
        "ec": None,
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "N7004 (NAD+)",
             "price": "~$25 / 1 g",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/n7004",
             "availability": "commercial"},
            {"name": "Sigma-Aldrich (Merck)", "sku": "N0505 (NADP+)",
             "price": "~$42 / 100 mg",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/n0505",
             "availability": "commercial"},
        ],
        "note": "Use with GDH/glucose regeneration — stoichiometric use is prohibitively expensive.",
    },
    {
        "aliases": ["terpene synthase", "terpenoid cyclase", "sesquiterpene synthase",
                    "diterpene synthase", "2.5.1", "terpene cyclase"],
        "name": "Terpene/Terpenoid Cyclase (various EC 2.5.1.x)",
        "ec": "2.5.1.x",
        "suppliers": [],
        "availability": "research/custom expression only — NOT commercially available as kits",
        "note": (
            "Must be expressed recombinantly (E. coli or P. pastoris). "
            "Custom protein expression services: Genscript ~$1500–3500 (4–8 weeks), "
            "ProteinTech ~$800–2000. Plasmid-only options: Addgene (free/nominal fee). "
            "Consider cell-free expression kits (Sigma CECF kit ~$400) for mg-scale."
        ),
    },
    {
        "aliases": ["hrp", "horseradish peroxidase", "1.11.1.7", "p6782"],
        "name": "Horseradish Peroxidase (HRP)",
        "ec": "1.11.1.7",
        "suppliers": [
            {"name": "Sigma-Aldrich (Merck)", "sku": "P6782",
             "price": "~$38 / 25 mg (lyophilised)",
             "url": "https://www.sigmaaldrich.com/catalog/product/sigma/p6782",
             "availability": "commercial"},
        ],
        "note": "Requires H2O2. Not suitable for regioselective C-H hydroxylation of terpenoids.",
    },
]


def _enzyme_cost_lookup(query: str) -> tuple[str, bool]:
    q = query.lower().strip()
    matches = []
    for entry in _ENZYME_CATALOG:
        if any(alias in q or q in alias for alias in entry["aliases"]):
            matches.append(entry)

    if not matches:
        # partial word match fallback
        words = set(q.split())
        for entry in _ENZYME_CATALOG:
            alias_words = set(" ".join(entry["aliases"]).split())
            if words & alias_words:
                matches.append(entry)

    if not matches:
        return json.dumps({
            "query": query,
            "status": "not_found",
            "message": (
                "Enzyme not in static catalog. "
                "Search suggestions: (1) use web_search for '<enzyme name> sigma-aldrich price'; "
                "(2) check Prozomix catalog at prozomix.com/products; "
                "(3) for recombinant-only enzymes, quote ~$800–3500 for custom expression service."
            ),
            "catalog_entries": [e["name"] for e in _ENZYME_CATALOG],
        }, indent=2), True

    results = []
    for e in matches[:3]:
        results.append({
            "name": e["name"],
            "ec": e.get("ec"),
            "uniprot": e.get("uniprot"),
            "suppliers": e.get("suppliers", []),
            "availability": e.get("availability", "see suppliers"),
            "note": e.get("note", ""),
            "cofactors": e.get("cofactors", []),
        })
    return json.dumps({"query": query, "status": "found", "results": results}, indent=2), True


# ---------------------------------------------------------------------------
# pains_alerts — PAINS / Brenk / NIH structural alert screening
# ---------------------------------------------------------------------------

def _pains_alerts(smiles: str) -> tuple[str, bool]:
    try:
        from rdkit import Chem
        from rdkit.Chem.FilterCatalog import FilterCatalog, FilterCatalogParams
    except ImportError:
        return "RDKit is not installed. Run: pip install rdkit", False

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return f"Invalid SMILES: {smiles!r}", False

    alerts: list[dict] = []

    def _run_catalog(catalog_enum, filter_name: str, severity: str) -> None:
        params = FilterCatalogParams()
        params.AddCatalog(catalog_enum)
        cat = FilterCatalog(params)
        for entry in cat.GetMatches(mol):
            alerts.append({
                "filter": filter_name,
                "name": entry.GetDescription(),
                "severity": severity,
            })

    _run_catalog(FilterCatalogParams.FilterCatalogs.PAINS_A, "PAINS-A", "high")
    _run_catalog(FilterCatalogParams.FilterCatalogs.PAINS_B, "PAINS-B", "high")
    _run_catalog(FilterCatalogParams.FilterCatalogs.PAINS_C, "PAINS-C", "high")
    _run_catalog(FilterCatalogParams.FilterCatalogs.BRENK,   "Brenk",   "medium")
    _run_catalog(FilterCatalogParams.FilterCatalogs.NIH,     "NIH",     "medium")

    status = "CLEAN" if not alerts else "FLAGGED"
    pains_count = sum(1 for a in alerts if a["filter"].startswith("PAINS"))
    recommendation = (
        "No structural alerts — compound can proceed to ADMET profiling."
        if not alerts else
        (
            f"{pains_count} PAINS alert(s) found: scaffold redesign required before "
            "biological testing (PAINS are assay artefacts). "
            if pains_count else ""
        ) + (
            f"{len(alerts) - pains_count} Brenk/NIH alert(s): check for metabolic "
            "liabilities or reactive groups."
            if len(alerts) - pains_count else ""
        )
    )
    return json.dumps({
        "smiles": smiles,
        "status": status,
        "n_alerts": len(alerts),
        "pains_hits": pains_count,
        "brenk_nih_hits": len(alerts) - pains_count,
        "alerts": alerts,
        "recommendation": recommendation,
    }, indent=2), True


# ---------------------------------------------------------------------------
# rdkit_scaffold — Bemis-Murcko scaffold + bioisostere suggestions
# ---------------------------------------------------------------------------

def _rdkit_scaffold(smiles: str, reference_smiles: str = None) -> tuple[str, bool]:
    try:
        from rdkit import Chem
        from rdkit.Chem.Scaffolds import MurckoScaffold
        from rdkit.Chem import rdMolDescriptors, rdFMCS
    except ImportError:
        return "RDKit is not installed. Run: pip install rdkit", False

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return f"Invalid SMILES: {smiles!r}", False

    scaffold = MurckoScaffold.GetScaffoldForMol(mol)
    scaffold_smi = Chem.MolToSmiles(scaffold) if scaffold else "no_scaffold"
    generic = MurckoScaffold.MakeScaffoldGeneric(scaffold) if scaffold else None
    generic_smi = Chem.MolToSmiles(generic) if generic else "N/A"

    n_rings = rdMolDescriptors.CalcNumRings(scaffold) if scaffold else 0
    n_arom  = rdMolDescriptors.CalcNumAromaticRings(scaffold) if scaffold else 0
    framework_type = (
        "aromatic" if n_arom == n_rings and n_arom > 0
        else "mixed" if n_arom > 0
        else "saturated"
    )
    scaffold_heavy = scaffold.GetNumHeavyAtoms() if scaffold else 0

    # Bioisostere suggestions based on substructures in the full molecule
    bioisosteres: list[dict] = []
    _smarts = Chem.MolFromSmarts

    checks = [
        ("c1ccccc1",    "phenyl",          "pyridyl (c1ccncc1), pyrimidyl (c1ccncn1), or pyridazinyl",
         "Reduces logP, adds H-bond acceptor, often improves solubility and metabolic stability"),
        ("[NH]",        "N-H (amide/NH)",   "O (ester/ether) or N-Me",
         "Lowers H-bond donor count → better oral absorption and BBB penetration"),
        ("C(=O)[OH]",  "carboxylic acid",  "tetrazole (c1nn[nH]n1-R) or hydroxamic acid (C(=O)NO)",
         "Tetrazole is pKa-matched (≈4.9 vs 4.5) and more lipophilic; reduces efflux"),
        ("C(=O)N",     "amide",            "E-alkene, 1,2,3-triazole (c1cn[nH]n1), or oxazole",
         "Reduces hydrolytic lability; triazole is CYP-resistant and rigid"),
        ("c1ccc(Cl)cc1","4-chlorophenyl",  "4-fluorophenyl or 3,4-difluorophenyl",
         "F is ≈3× smaller than Cl, avoids para-CYP oxidation, improved metabolic stability"),
        ("S(=O)(=O)N", "sulfonamide",      "acylsulfonamide or phosphonamide",
         "Maintains acidic NH but modulates pKa; acylsulfonamides more metabolically stable"),
        ("c1cc[nH]c1", "pyrrole",          "indole, benzimidazole, or azaindole",
         "Aromatic NH is a mutagenicity risk (Ames); ring fusion reduces planarity alert"),
        ("C#N",        "nitrile",          "tetrazole, amide, or amino-oxazole",
         "Nitrile can be metabolised to cyanide; tetrazole avoids CYP2C19 metabolite liability"),
    ]
    for smarts, original, replacement, rationale in checks:
        pat = _smarts(smarts)
        if pat and mol.HasSubstructMatch(pat):
            bioisosteres.append({
                "substructure": original,
                "replacement_options": replacement,
                "rationale": rationale,
            })

    result: dict = {
        "query_smiles": smiles,
        "bemis_murcko_scaffold": scaffold_smi,
        "generic_scaffold": generic_smi,
        "scaffold_heavy_atoms": scaffold_heavy,
        "side_chain_heavy_atoms": mol.GetNumHeavyAtoms() - scaffold_heavy,
        "framework_rings": n_rings,
        "aromatic_rings": n_arom,
        "framework_type": framework_type,
        "bioisostere_suggestions": bioisosteres,
    }

    if reference_smiles:
        ref_mol = Chem.MolFromSmiles(reference_smiles)
        if ref_mol:
            mcs = rdFMCS.FindMCS(
                [mol, ref_mol],
                completeRingsOnly=True,
                ringMatchesRingOnly=True,
                timeout=5,
            )
            result["mcs_with_reference"] = {
                "mcs_smarts": mcs.smartsString,
                "mcs_atoms": mcs.numAtoms,
                "mcs_bonds": mcs.numBonds,
                "query_unique_atoms": mol.GetNumHeavyAtoms() - mcs.numAtoms,
                "ref_unique_atoms": ref_mol.GetNumHeavyAtoms() - mcs.numAtoms,
                "note": "Atoms outside MCS are the scaffold-hop delta — prime bioisostere candidates",
            }

    return json.dumps(result, indent=2), True


# ---------------------------------------------------------------------------
# swissadme_fetch — SwissADME ADMET prediction
# ---------------------------------------------------------------------------

def _swissadme_fetch(smiles: str) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    import urllib.parse

    encoded = urllib.parse.quote(smiles, safe="")
    try:
        r = _requests.post(
            "http://www.swissadme.ch/include/sendQuery.php",
            data={"smi": smiles},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "OctoSlave/0.1 (research agent)",
            },
            allow_redirects=True,
            timeout=30,
        )
    except Exception as e:
        return json.dumps({
            "status": "connection_error",
            "message": f"SwissADME unreachable: {e}",
            "fallback": "Use rdkit_admet for local ADMET prediction (no internet required)",
            "swissadme_url": f"http://www.swissadme.ch/?smi={encoded}",
        }, indent=2), False

    if r.status_code != 200:
        return json.dumps({
            "status": "http_error",
            "code": r.status_code,
            "message": "SwissADME returned non-200 response. Use rdkit_admet as fallback.",
            "swissadme_url": f"http://www.swissadme.ch/?smi={encoded}",
        }, indent=2), False

    # Try to parse key properties from the HTML response
    import re
    text = r.text
    props: dict = {}

    patterns = [
        ("consensus_logP",   r'Consensus Log Po/w[^0-9\-]*(-?[0-9]+\.?[0-9]*)'),
        ("GI_absorption",    r'GI absorption[^A-Za-z]*(High|Low)'),
        ("BBB_permeant",     r'BBB permeant[^A-Za-z]*(Yes|No)'),
        ("Pgp_substrate",    r'P-gp substrate[^A-Za-z]*(Yes|No)'),
        ("CYP1A2_inhibitor", r'CYP1A2 inhibitor[^A-Za-z]*(Yes|No)'),
        ("CYP2C19_inhibitor",r'CYP2C19 inhibitor[^A-Za-z]*(Yes|No)'),
        ("CYP2C9_inhibitor", r'CYP2C9 inhibitor[^A-Za-z]*(Yes|No)'),
        ("CYP2D6_inhibitor", r'CYP2D6 inhibitor[^A-Za-z]*(Yes|No)'),
        ("CYP3A4_inhibitor", r'CYP3A4 inhibitor[^A-Za-z]*(Yes|No)'),
        ("lipinski_ro5",     r'Lipinski[^A-Za-z]*(Yes|No)'),
        ("veber_oral",       r'Veber[^A-Za-z]*(Yes|No)'),
        ("PAINS_alerts",     r'PAINS[^0-9]*([0-9]+)\s*alert'),
        ("synthetic_access", r'Synthetic accessibility[^0-9]*([0-9]+\.?[0-9]*)'),
    ]
    for key, pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1)
            try:
                props[key] = float(val)
            except ValueError:
                props[key] = val

    if props:
        return json.dumps({
            "status": "success",
            "source": "SwissADME (swissadme.ch)",
            "smiles": smiles,
            "properties": props,
            "full_result_url": f"http://www.swissadme.ch/?smi={encoded}",
        }, indent=2), True

    # Fallback: return URL only
    return json.dumps({
        "status": "parse_limited",
        "message": "SwissADME responded but HTML structure could not be parsed. "
                   "Visit the URL for full results, or use rdkit_admet for local prediction.",
        "smiles": smiles,
        "full_result_url": f"http://www.swissadme.ch/?smi={encoded}",
        "fallback": "rdkit_admet",
    }, indent=2), True


# ---------------------------------------------------------------------------
# pkcsm_fetch — pkCSM ADMET numerical predictions
# ---------------------------------------------------------------------------

_PKCSM_ENDPOINTS = {
    "absorption":    "/absorption/predict",
    "distribution":  "/distribution/predict",
    "metabolism":    "/metabolism/predict",
    "excretion":     "/excretion/predict",
    "toxicity":      "/toxicity/predict",
}
_PKCSM_BASE = "https://biosig.unimelb.edu.au/pkcsm/api"


def _pkcsm_fetch(smiles: str, endpoint: str = "all") -> tuple[str, bool]:
    if err := _need_requests():
        return err

    if endpoint == "all":
        targets = list(_PKCSM_ENDPOINTS.items())
    elif endpoint in _PKCSM_ENDPOINTS:
        targets = [(endpoint, _PKCSM_ENDPOINTS[endpoint])]
    else:
        return (
            f"Unknown endpoint {endpoint!r}. "
            f"Choose from: all, {', '.join(_PKCSM_ENDPOINTS)}", False
        )

    predictions: dict = {}
    errors: dict = {}
    for ep_name, ep_path in targets:
        try:
            r = _requests.post(
                _PKCSM_BASE + ep_path,
                data={"smiles": smiles},
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "User-Agent": "OctoSlave/0.1"},
                timeout=30,
            )
            if r.status_code == 200:
                try:
                    predictions[ep_name] = r.json()
                except Exception:
                    predictions[ep_name] = {"raw_text": r.text[:500]}
            else:
                errors[ep_name] = f"HTTP {r.status_code}"
        except Exception as e:
            errors[ep_name] = str(e)

    return json.dumps({
        "smiles": smiles,
        "endpoint": endpoint,
        "source": "pkCSM (biosig.unimelb.edu.au/pkcsm)",
        "citation": "Pires et al. (2015) J. Med. Chem. 58(9):4066-4072",
        "predictions": predictions,
        "errors": errors if errors else None,
    }, indent=2), bool(predictions)


# ---------------------------------------------------------------------------
# surechembl_search — patent landscape via SureChEMBL
# ---------------------------------------------------------------------------

def _surechembl_search(
    smiles: str = None,
    query: str = None,
    similarity: float = 0.85,
    limit: int = 10,
) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    if not smiles and not query:
        return "Provide either smiles or query.", False

    limit = max(1, min(int(limit or 10), 50))
    similarity = float(similarity or 0.85)

    import urllib.parse

    try:
        if smiles:
            params = {
                "smiles": smiles,
                "type": "similarity",
                "threshold": int(similarity * 100),
                "limit": limit,
            }
            r = _http_get("https://www.surechembl.org/search/chemical", params=params)
        else:
            params = {"query": query, "limit": limit}
            r = _http_get("https://www.surechembl.org/search/text", params=params)
    except Exception as e:
        return json.dumps({
            "status": "connection_error",
            "message": f"SureChEMBL unreachable: {e}",
            "fallback": "Use web_search: site:surechembl.org " + (smiles or query or ""),
        }, indent=2), False

    patents: list[dict] = []
    if r.status_code == 200:
        try:
            data = r.json()
            raw = data.get("results") or data.get("patents") or data.get("hits") or []
            for item in raw[:limit]:
                pid = item.get("patent_number") or item.get("id") or item.get("doc_id")
                patents.append({
                    "patent_id": pid,
                    "title": (item.get("title") or "")[:200],
                    "publication_date": item.get("publication_date") or item.get("date"),
                    "assignee": item.get("assignee") or item.get("applicant"),
                    "url": f"https://www.surechembl.org/document/{pid}" if pid else None,
                })
        except Exception:
            pass

    if not patents:
        encoded = urllib.parse.quote(smiles or query or "", safe="")
        return json.dumps({
            "status": "no_results_or_unavailable",
            "query_smiles": smiles,
            "query_text": query,
            "patents": [],
            "recommendation": "No results from SureChEMBL API. Verify manually.",
            "manual_urls": [
                f"https://www.surechembl.org/search/#q={encoded}",
                "https://worldwide.espacenet.com/patent/search",
                "https://patents.google.com",
            ],
        }, indent=2), True

    ip_flag = (
        f"Found {len(patents)} patent(s). Recommend FTO (freedom-to-operate) analysis "
        "before progressing this scaffold, especially if >3 patents cover the same core."
        if len(patents) >= 3 else
        f"Found {len(patents)} patent(s). Low IP density — scaffold may have design freedom."
    )
    return json.dumps({
        "query_smiles": smiles,
        "query_text": query,
        "similarity_cutoff": similarity if smiles else None,
        "n_results": len(patents),
        "patents": patents,
        "ip_assessment": ip_flag,
    }, indent=2), True


# ---------------------------------------------------------------------------
# vina_dock — AutoDock Vina / Smina / Gnina docking wrapper
# ---------------------------------------------------------------------------

def _vina_dock(
    ligand_smiles: str,
    receptor_source: str,
    pocket_x: float,
    pocket_y: float,
    pocket_z: float,
    box_size_x: float = 20.0,
    box_size_y: float = 20.0,
    box_size_z: float = 20.0,
    exhaustiveness: int = 8,
    n_poses: int = 5,
    working_dir: str = ".",
) -> tuple[str, bool]:
    import subprocess
    import shutil

    # Check for docking binary
    vina_bin = None
    for candidate in ("vina", "smina", "gnina"):
        if shutil.which(candidate):
            vina_bin = candidate
            break

    if vina_bin is None:
        return json.dumps({
            "status": "binary_not_found",
            "message": (
                "No docking binary found (vina / smina / gnina). "
                "Install with: conda install -c conda-forge vina  "
                "or: pip install vina"
            ),
            "install_options": [
                "conda install -c conda-forge vina",
                "conda install -c conda-forge smina",
                "pip install vina",
            ],
        }, indent=2), False

    if not shutil.which("obabel"):
        return json.dumps({
            "status": "obabel_not_found",
            "message": (
                "obabel is required for PDBQT conversion. "
                "Install with: conda install -c conda-forge openbabel"
            ),
        }, indent=2), False

    dock_dir = Path(working_dir) / "docking_tmp"
    dock_dir.mkdir(parents=True, exist_ok=True)

    # Resolve receptor
    receptor_pdbqt = dock_dir / "receptor.pdbqt"
    receptor_pdb = None

    if len(receptor_source) == 4 and receptor_source.replace("_", "").isalnum():
        # PDB ID — download
        pdb_out = dock_dir / f"{receptor_source.lower()}.pdb"
        dl_result, dl_ok = _pdb_fetch(
            pdb_id=receptor_source.upper(),
            format="pdb",
            output_path=str(pdb_out),
            working_dir=working_dir,
        )
        if not dl_ok:
            return f"Failed to download PDB {receptor_source}: {dl_result}", False
        receptor_pdb = pdb_out
    else:
        candidate = Path(receptor_source)
        if not candidate.is_absolute():
            candidate = Path(working_dir) / receptor_source
        if not candidate.exists():
            return f"Receptor file not found: {receptor_source}", False
        if candidate.suffix == ".pdbqt":
            receptor_pdbqt = candidate
            receptor_pdb = None  # skip conversion
        else:
            receptor_pdb = candidate

    # Convert receptor PDB → PDBQT (strip HETATM/water, add H, Gasteiger charges)
    if receptor_pdb is not None:
        conv = subprocess.run(
            ["obabel", str(receptor_pdb), "-O", str(receptor_pdbqt),
             "-xr", "--partialcharge", "gasteiger", "--delete", "HOH"],
            capture_output=True, text=True, timeout=120,
        )
        if not receptor_pdbqt.exists():
            return (
                f"Receptor PDBQT conversion failed.\n"
                f"obabel stdout: {conv.stdout[:400]}\n"
                f"obabel stderr: {conv.stderr[:400]}"
            ), False

    # Convert ligand SMILES → 3D → PDBQT
    ligand_pdbqt = dock_dir / "ligand.pdbqt"
    lig_conv = subprocess.run(
        ["obabel", f"-:{ligand_smiles}", "--gen3d", "-O", str(ligand_pdbqt),
         "--partialcharge", "gasteiger"],
        capture_output=True, text=True, timeout=60,
    )
    if not ligand_pdbqt.exists():
        return (
            f"Ligand PDBQT preparation failed.\n"
            f"obabel stdout: {lig_conv.stdout[:400]}\n"
            f"obabel stderr: {lig_conv.stderr[:400]}"
        ), False

    # Write Vina config
    config_path = dock_dir / "vina.conf"
    out_pdbqt   = dock_dir / "docked.pdbqt"
    config_path.write_text(
        f"receptor = {receptor_pdbqt}\n"
        f"ligand   = {ligand_pdbqt}\n"
        f"center_x = {pocket_x}\ncenter_y = {pocket_y}\ncenter_z = {pocket_z}\n"
        f"size_x   = {box_size_x}\nsize_y   = {box_size_y}\nsize_z   = {box_size_z}\n"
        f"exhaustiveness = {exhaustiveness}\nnum_modes = {n_poses}\n"
    )

    # Run docking
    vina_run = subprocess.run(
        [vina_bin, "--config", str(config_path), "--out", str(out_pdbqt)],
        capture_output=True, text=True, timeout=600,
    )

    # Parse affinity table from stdout
    poses: list[dict] = []
    for line in vina_run.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            try:
                poses.append({
                    "pose": int(parts[0]),
                    "affinity_kcal_mol": float(parts[1]),
                    "rmsd_lb_A": float(parts[2]) if len(parts) > 2 else None,
                    "rmsd_ub_A": float(parts[3]) if len(parts) > 3 else None,
                })
            except (ValueError, IndexError):
                pass

    if not poses and vina_run.returncode != 0:
        return (
            f"Docking failed (exit {vina_run.returncode}).\n"
            f"STDOUT: {vina_run.stdout[:500]}\nSTDERR: {vina_run.stderr[:500]}"
        ), False

    best = poses[0]["affinity_kcal_mol"] if poses else None
    interpretation = (
        "No poses generated"
        if not poses else
        "Very potent (≤ −9 kcal/mol)" if best <= -9 else
        "Moderate binding (−7 to −9 kcal/mol)" if best <= -7 else
        "Weak binding (−5 to −7 kcal/mol)" if best <= -5 else
        "Poor binding (> −5 kcal/mol)"
    )

    return json.dumps({
        "status": "success",
        "docking_binary": vina_bin,
        "ligand_smiles": ligand_smiles,
        "receptor": receptor_source,
        "pocket_center": {"x": pocket_x, "y": pocket_y, "z": pocket_z},
        "box_angstrom": {"x": box_size_x, "y": box_size_y, "z": box_size_z},
        "exhaustiveness": exhaustiveness,
        "top_poses": poses,
        "best_affinity_kcal_mol": best,
        "interpretation": interpretation,
        "output_pdbqt": str(out_pdbqt),
        "affinity_guide": {
            "very_potent": "≤ −9 kcal/mol",
            "moderate":    "−7 to −9 kcal/mol",
            "weak":        "−5 to −7 kcal/mol",
            "poor":        "> −5 kcal/mol",
        },
    }, indent=2), True


# ---------------------------------------------------------------------------
# bindingdb_lookup — off-target affinity data from BindingDB
# ---------------------------------------------------------------------------

def _bindingdb_lookup(
    smiles: str = None,
    target_gene: str = None,
    ki_cutoff_nm: float = 10000.0,
    limit: int = 10,
) -> tuple[str, bool]:
    if err := _need_requests():
        return err
    if not smiles and not target_gene:
        return "Provide either smiles or target_gene.", False

    limit = max(1, min(int(limit or 10), 50))
    ki_cutoff = float(ki_cutoff_nm or 10000.0)
    results: list[dict] = []

    try:
        if target_gene:
            # Search by target gene/protein name
            r = _http_get(
                "https://bindingdb.org/rwd/bind/BDBClient/getTargets.json",
                params={"targetname": target_gene, "response": "json"},
            )
            if r.status_code == 200:
                data = r.json()
                affinities = (
                    data.get("affinities") or
                    data.get("results") or
                    data.get("bdb_affinities") or []
                )
                for entry in affinities:
                    for aff_key in ("ki", "ic50", "kd", "ec50", "affinity_nm"):
                        raw = entry.get(aff_key)
                        if raw is None:
                            continue
                        try:
                            val = float(str(raw).replace(">", "").replace("<", "").strip())
                        except ValueError:
                            continue
                        if val <= ki_cutoff:
                            results.append({
                                "compound": entry.get("ligand_name") or entry.get("compound_name"),
                                "smiles": entry.get("ligand_smiles"),
                                "target": target_gene,
                                "affinity_nM": val,
                                "affinity_type": aff_key.upper(),
                                "assay": entry.get("assay_description"),
                                "pmid": entry.get("pmid") or entry.get("doi"),
                            })
                            break

        if smiles and len(results) < limit:
            # SMILES similarity search
            import urllib.parse
            r2 = _http_get(
                "https://bindingdb.org/rwd/bind/chemsearch/marvin/MolStructure.jsp",
                params={
                    "action": "GetTargets",
                    "smiles": smiles,
                    "threshold": "85",
                    "response": "json",
                },
            )
            if r2.status_code == 200:
                data2 = r2.json()
                for entry in (data2.get("affinities") or []):
                    for aff_key in ("ki", "ic50", "kd", "ec50"):
                        raw = entry.get(aff_key)
                        if raw is None:
                            continue
                        try:
                            val = float(str(raw).replace(">", "").replace("<", "").strip())
                        except ValueError:
                            continue
                        if val <= ki_cutoff:
                            results.append({
                                "compound": entry.get("ligand_smiles", smiles),
                                "target": entry.get("target_name") or entry.get("gene_name"),
                                "affinity_nM": val,
                                "affinity_type": aff_key.upper(),
                                "tanimoto_similarity": entry.get("similarity"),
                                "pmid": entry.get("pmid"),
                            })
                            break

    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"BindingDB request failed: {e}",
            "fallback": (
                "Use chembl_lookup with target gene name, or "
                "web_search 'BindingDB [compound name]' for off-target data"
            ),
        }, indent=2), False

    results.sort(key=lambda x: x.get("affinity_nM", 9e9))
    results = results[:limit]

    off_target_note = (
        "No binding data found. Novel scaffold or not in BindingDB. "
        "Verify with ChEMBL or PubChem BioAssay."
        if not results else
        f"Found {len(results)} binding event(s) ≤ {ki_cutoff:.0f} nM. "
        "Review targets for selectivity risks (CYPs, hERG, kinase family)."
    )

    return json.dumps({
        "query_smiles": smiles,
        "query_target": target_gene,
        "ki_cutoff_nm": ki_cutoff,
        "n_results": len(results),
        "off_target_note": off_target_note,
        "affinities": results,
    }, indent=2), True
