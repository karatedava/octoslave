"""
Minimal v0 peptide descriptor module for anticancer peptide proof-of-concept.

Scope (per Director synthesis):
- Natural L-amino acids only.
- Binary N-acetyl / C-amidation flags (converted to terminus-neutralizing
  adjustments for net charge but otherwise ignored for structural proxies).
- D-amino acids, non-natural residues, cyclization recorded in
  'modification_flags' are EXCLUDED from v0 descriptor and rule calculations.

Computed descriptors:
  - length
  - molecular weight (avg isotopic, free + termini + modifications)
  - amino-acid composition fractions (20 standard residues)
  - net charge at pH 7.4 (Henderson-Hasselbalch, pKa set documented inline)
  - GRAVY (Kyte-Doolittle average hydropathy)
  - hydrophobic moment (Eisenberg consensus scale, angle 100°, window=length)
  - helicity proxy = mean helical propensity via AA-scale approximation
  - toxicity liability flags (hydrophobic burden, charge, face imbalance)

Reproducibility: every public run appends JSONL metadata.
"""

from __future__ import annotations

import json
import hashlib
import datetime
import math
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


STD_AA = set("ACDEFGHIKLMNPQRSTVWY")

# Average residue molecular weights (residue inside chain, after water loss)
# plus free termini used to compute full MW.
AA_MW_RESIDUE = {
    "A": 71.08,
    "C": 103.14,
    "D": 115.09,
    "E": 129.12,
    "F": 147.18,
    "G": 57.05,
    "H": 137.14,
    "I": 113.16,
    "K": 128.17,
    "L": 113.16,
    "M": 131.19,
    "N": 114.10,
    "P": 97.12,
    "Q": 128.13,
    "R": 156.19,
    "S": 87.08,
    "T": 101.11,
    "V": 99.13,
    "W": 186.21,
    "Y": 163.18,
}
WATER_MW = 18.015

# Kyte-Doolittle hydropathy index (Kyte J, Doolittle RF, J Mol Biol 1982)
KYTE_DOOLITTLE = {
    "I": 4.5,
    "V": 4.2,
    "L": 3.8,
    "F": 2.8,
    "C": 2.5,
    "M": 1.9,
    "A": 1.8,
    "G": -0.4,
    "T": -0.7,
    "S": -0.8,
    "W": -0.9,
    "Y": -1.3,
    "P": -1.6,
    "H": -3.2,
    "E": -3.5,
    "Q": -3.5,
    "D": -3.5,
    "N": -3.5,
    "K": -3.9,
    "R": -4.5,
}

# Eisenberg consensus hydrophobicity scale (Eisenberg et al., 1984)
EISENBERG = {
    "I": 0.73,
    "F": 0.61,
    "C": 0.33,
    "L": 0.53,
    "V": 0.54,
    "M": 0.26,
    "G": 0.00,
    "A": 0.25,
    "T": -0.18,
    "W": 0.37,
    "Y": 0.02,
    "P": -0.07,
    "S": -0.26,
    "H": -0.40,
    "E": -0.62,
    "N": -0.64,
    "Q": -0.69,
    "D": -0.77,
    "K": -1.10,
    "R": -1.80,
}

# pKa values used for net charge at pH 7.4 (canonical free peptide)
# Source: Lehninger / CRC consensus approximations.
PKA_NTERM = 9.69
PKA_CTERM = 2.34
PKA_SIDE = {
    "D": 3.65,
    "E": 4.25,
    "C": 8.33,
    "Y": 10.07,
    "H": 6.00,
    "K": 10.53,
    "R": 12.48,
}

# Helical propensity proxy: normalized到家 helix propensity scale (O'Neil/DeGrado)
# Higher = more helical.
HELIX_PROPENSITY = {
    "A": 1.45,
    "R": 0.79,
    "N": 0.73,
    "D": 0.76,
    "C": 0.77,
    "Q": 0.89,
    "E": 1.17,
    "G": 0.53,
    "H": 0.76,
    "I": 1.07,
    "L": 1.34,
    "K": 0.97,
    "M": 1.20,
    "F": 1.16,
    "P": 0.59,
    "S": 0.75,
    "T": 0.76,
    "W": 1.14,
    "Y": 0.99,
    "V": 0.99,
}


class DescriptorError(ValueError):
    pass


def validate_sequence(seq: str, allowed_modifications: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Validate v0-scope sequence. Return canonical sequence and error if any.
    Non-natural residues / D-aa / cyclization must be encoded in modification_flags
    and are not accepted inside sequence for descriptor calculations.
    """
    if not seq:
        return seq, "empty sequence"
    bad = [aa for aa in seq if aa not in STD_AA]
    if bad:
        return seq, f"non-standard residues in sequence: {set(bad)}"
    return seq, None


def compute_length(seq: str) -> int:
    return len(seq)


def compute_molecular_weight(seq: str, is_amidated: bool = False, is_acetylated: bool = False) -> float:
    """
    Average molecular weight of the (approximately linear) peptide form.
    C-amidation replaces C-term OH with NH2 (subtract OH, add NH2? net same as
    replacing O with NH: +15.015 - 16.0 = -0.985). N-acetylation replaces H
    with COCH3 (add 42.04 - 1.01 = +41.03). Values are approximate averages.
    """
    if not seq:
        return 0.0
    mw = sum(AA_MW_RESIDUE[aa] for aa in seq) + WATER_MW
    if is_amidated:
        mw += 15.015 - 16.000  # ~ -0.985
    if is_acetylated:
        mw += 42.04 - 1.008   # ~ +41.03
    return round(mw, 3)


def compute_composition(seq: str) -> Dict[str, float]:
    n = len(seq) or 1
    return {aa: round(seq.count(aa) / n, 4) for aa in sorted(STD_AA)}


def _charge_group(pka: float, charge_sign: float, ph: float) -> float:
    """Henderson-Hasselbalch fractional charge contribution."""
    fraction = 1.0 / (1.0 + 10 ** (charge_sign * (ph - pka)))
    return charge_sign * fraction


def compute_net_charge(seq: str, ph: float = 7.4,
                       is_amidated: bool = False,
                       is_acetylated: bool = False) -> float:
    """
    Net charge via Henderson-Hasselbalch.
    N-term and C-term contributions are neutralized by acetylation/amidation.
    """
    if not seq:
        return 0.0
    charge = 0.0
    # termini
    if not is_acetylated:
        charge += _charge_group(PKA_NTERM, +1.0, ph)
    if not is_amidated:
        charge += _charge_group(PKA_CTERM, -1.0, ph)
    # side chains
    for aa, pka in PKA_SIDE.items():
        count = seq.count(aa)
        if count:
            sign = +1.0 if aa in {"K", "R", "H"} else -1.0
            charge += count * _charge_group(pka, sign, ph)
    return round(charge, 3)


def compute_gravy(seq: str) -> float:
    if not seq:
        return 0.0
    return round(sum(KYTE_DOOLITTLE[aa] for aa in seq) / len(seq), 4)


def compute_hydrophobic_moment(seq: str, angle_deg: float = 100.0) -> float:
    """Overall hydrophobic moment for the whole sequence (single window = length)."""
    if not seq:
        return 0.0
    angle = math.radians(angle_deg)
    msum = complex(0, 0)
    for i, aa in enumerate(seq):
        h = EISENBERG[aa]
        msum += h * complex(math.cos(i * angle), math.sin(i * angle))
    return round(abs(msum) / len(seq), 4)


def compute_helicity_proxy(seq: str) -> float:
    """Mean helical propensity scaled 0-1-ish (raw average / 1.5)."""
    if not seq:
        return 0.0
    avg = sum(HELIX_PROPENSITY[aa] for aa in seq) / len(seq)
    return round(min(avg / 1.5, 1.0), 4)


def compute_toxicity_flags(seq: str, net_charge: float, gravy: float,
                           hydrophobic_moment: float) -> Dict[str, int]:
    """
    Crude liability markers informed by AMP/hemolysis literature (Hoskin &
    Ramamoorthy; iAMP-HL/HemoPI-like coarse rules). Flags are binary clues, not
    validated toxicity predictions.
    """
    n = len(seq) or 1
    hydrophobic_frac = sum(1 for aa in seq if KYTE_DOOLITTLE[aa] > 0) / n
    charge_flag = 1 if net_charge >= 4.0 else 0
    hydrophobic_flag = 1 if hydrophobic_frac >= 0.40 else 0
    moment_flag = 1 if hydrophobic_moment >= 0.35 else 0
    # Amphipathic face imbalance: hydrophobic moment high AND high net charge
    amphipathic_flag = 1 if (moment_flag and charge_flag) else 0
    combined_flag = 1 if (charge_flag + hydrophobic_flag + moment_flag) >= 2 else 0
    return {
        "hydrophobic_frac": round(hydrophobic_frac, 4),
        "flag_charge_high": charge_flag,
        "flag_hydrophobic_high": hydrophobic_flag,
        "flag_hydrophobic_moment_high": moment_flag,
        "flag_amphipathic": amphipathic_flag,
        "flag_combined_tox_risk": combined_flag,
    }


def compute_all(seq: str,
                is_amidated: bool = False,
                is_acetylated: bool = False,
                peptide_id: str = "",
                modification_flags: Optional[str] = None,
                ) -> Dict:
    """Compute full v0 descriptor row."""
    canon, err = validate_sequence(seq, modification_flags)
    if err:
        raise DescriptorError(err)

    descriptors = {
        "peptide_id": peptide_id,
        "canonical_sequence": canon,
        "is_amidated": int(is_amidated),
        "is_acetylated": int(is_acetylated),
        "modification_flags_excluded": bool(modification_flags),
        "length": compute_length(canon),
        "molecular_weight": compute_molecular_weight(canon, is_amidated, is_acetylated),
    }
    descriptors.update({"comp_" + aa: v for aa, v in compute_composition(canon).items()})
    descriptors["net_charge_ph7_4"] = compute_net_charge(canon, 7.4, is_amidated, is_acetylated)
    descriptors["gravy"] = compute_gravy(canon)
    descriptors["hydrophobic_moment"] = compute_hydrophobic_moment(canon)
    descriptors["helicity_proxy"] = compute_helicity_proxy(canon)

    tox = compute_toxicity_flags(canon,
                                 descriptors["net_charge_ph7_4"],
                                 descriptors["gravy"],
                                 descriptors["hydrophobic_moment"])
    descriptors.update(tox)
    return descriptors


def descriptor_table(records: List[Dict], log_path: Optional[str] = None) -> Tuple[List[Dict], str]:
    """
    Compute descriptors for a list of peptide records and emit a JSONL log entry.
    records: dicts with keys canonical_sequence, is_amidated, is_acetylated,
             peptide_id, modification_flags (optional).
    Returns (rows, log_path).
    """
    rows = []
    for r in records:
        row = compute_all(
            seq=r["canonical_sequence"],
            is_amidated=bool(r.get("is_amidated", 0)),
            is_acetylated=bool(r.get("is_acetylated", 0)),
            peptide_id=r["peptide_id"],
            modification_flags=r.get("modification_flags") or None,
        )
        rows.append(row)

    if log_path is None:
        log_path = "logs/descriptors/v0_descriptors.jsonl"
    log_path = str(log_path)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    # Build log entry
    input_text = json.dumps(records, sort_keys=True)
    input_hash = hashlib.sha256(input_text.encode()).hexdigest()[:16]
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent": "PeptideAnalyst",
        "tool": "peptide_descriptors/v0/descriptors.py",
        "source_url": "module_run",
        "input_hash": input_hash,
        "output_file": log_path,
        "schema_version": "v0_descriptors",
        "git_commit": git_commit,
        "params": {
            "ph": 7.4,
            "hydrophobic_moment_angle_deg": 100,
            "modification_policy": "natural_L_aa_only;D_aa/cyclization/non_natural_excluded_from_descriptor_calculations",
        },
        "n_peptides": len(rows),
    }
    with open(log_path, "w") as f:
        f.write(json.dumps(entry) + "\n")

    return rows, log_path


if __name__ == "__main__":
    import json as _json
    raw_path = "data/raw/v0_dummy_peptides.json"
    with open(raw_path) as f:
        records = _json.load(f)
    rows, log = descriptor_table(records)
    print("Computed descriptors for", len(rows), "peptides; log:", log)
    for row in rows:
        print(row["peptide_id"], "len=", row["length"],
              "MW=", row["molecular_weight"],
              "charge=", row["net_charge_ph7_4"],
              "gravy=", row["gravy"],
              "muH=", row["hydrophobic_moment"],
              "helix=", row["helicity_proxy"])
