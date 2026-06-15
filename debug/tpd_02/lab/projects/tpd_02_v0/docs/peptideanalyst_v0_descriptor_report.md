# PeptideAnalyst v0 Descriptor & Baseline Report

**Agent:** PeptideAnalyst  
**Project:** lab/projects/tpd_02_v0  
**Date:** 2026-06-15  
**Git commit:** a2d2fd03131dd6e06600828bd8b47b8485f13ad2

---

## 1. Scope and Constraints

Following the Director synthesis, this v0 module deliberately operates on a **minimal, validated peptide form**:

- **Natural L-amino acids only** (one-letter uppercase codes).
- **Binary modifications only:** C-terminal amidation and N-terminal acetylation.
- **D-amino acids, non-natural residues, cyclization, and disulfides** are recorded in `modification_flags` but **excluded from descriptor and baseline calculations.**
  - Example: `clo_v0_05` (`RLLFKFFRCKWKF`) is cyclic/disulfide-constrained; only the linear residue string contributes to descriptors.

This scope trades completeness for validity and avoids unstated stereochemistry / conformation assumptions.

---

## 2. Descriptors Implemented

| Descriptor | Method | Notes |
|------------|--------|-------|
| `length` | Residue count | — |
| `molecular_weight` | Average residue MW + H₂O + amidation/acetylation mass deltas | Approximate average isotopic masses. |
| `comp_X` (X = 20 AA) | Fraction of each standard residue | — |
| `net_charge_ph7_4` | Henderson–Hasselbalch with documented pKa set | N-term pKa 9.69, C-term 2.34, side-chain pKa per Lehninger/CRC consensus; termini neutralized by amidation/acetylation. |
| `gravy` | Mean Kyte–Doolittle hydropathy | Kyte & Doolittle, JMB 1982. |
| `hydrophobic_moment` | Eisenberg consensus scale, 100° phase angle, whole-sequence window | Single global moment per peptide for v0; windowed moment deferred to v1. |
| `helicity_proxy` | Mean O'Neil/DeGrado helical propensity, scaled by 1.5 | Proxy only; not a physics-based folding prediction. |
| Toxicity flags | Literature-informed binary markers | `flag_charge_high`, `flag_hydrophobic_high`, `flag_hydrophobic_moment_high`, `flag_amphipathic`, `flag_combined_tox_risk`. |

---

## 3. Toxicity Liability Flags

Flags are **coarse liability markers**, not validated toxicity predictions. They are informed by the AMP/anticancer peptide literature (Hoskin & Ramamoorthy; HemoPI/iAMP-HL-style thresholds):

- `flag_charge_high`: net charge ≥ +4 at pH 7.4.
- `flag_hydrophobic_high`: hydrophobic residue fraction ≥ 40 %.
- `flag_hydrophobic_moment_high`: global hydrophobic moment ≥ 0.35.
- `flag_amphipathic`: high charge AND high hydrophobic moment (suggestive of membrane-active amphipathic helix).
- `flag_combined_tox_risk`: ≥2 of charge / hydrophobic / moment flags set.

**Important:** a combined flag does **not** mean the peptide is hemolytic; it means the physicochemical profile overlaps known hemolytic / non-selective antimicrobial peptides and should trigger a closer look at in vivo toxicity data.

---

## 4. Input Data

Source: `data/raw/v0_dummy_peptides.json` (dummy substitution until BioCurator selects a real v0 source).

| peptide_id | sequence | amidated | acetylated | label_active | endpoint_value_uM |
|---|---|---|---|---|---|
| tox_v0_01 | GIGKFLHSAKKFGKAFVGEIMNS | 1 | 0 | 1 | 4.2 |
| tox_v0_02 | GIGKFLHSAGKFGKAFVGEIMNS | 1 | 0 | 1 | 12.5 |
| tox_v0_03 | KWKLFKKIGIGKVLTTGLPALIS | 1 | 0 | 1 | 8.7 |
| tox_v0_04 | GLFDIVKKVVGALGSL | 1 | 1 | 1 | 25.0 |
| clo_v0_05 | RLLFKFFRCKWKF | 1 | 0 | 1 | 2.1 |
| neg_v0_06 | AAAAAA | 0 | 0 | 0 | — |
| neg_v0_07 | GGGGGG | 0 | 0 | 0 | — |
| neg_v0_08 | EEKKEE | 0 | 0 | 0 | — |

`clo_v0_05` carries `modification_flags = "N->C cyclic disulfide Cy5-Cy12"`; that flag is recorded but **not** used in descriptors.

---

## 5. Descriptor Output Summary

| peptide_id | length | MW | charge@pH7.4 | GRAVY | hydrophobic_moment | helicity_proxy | combined_tox_risk |
|---|---|---|---|---|---|---|---|
| tox_v0_01 | 23 | 2465.93 | +4.03 | 0.083 | 0.289 | 0.644 | 1 |
| tox_v0_02 | 23 | 2394.81 | +3.03 | 0.235 | 0.292 | 0.631 | 0 |
| tox_v0_03 | 23 | 2511.17 | +5.99 | 0.526 | 0.044 | 0.655 | 1 |
| tox_v0_04 | 16 | 1656.01 | +1.00 | 1.238 | 0.322 | 0.655 | 1 |
| clo_v0_05 | 13 | 1818.31 | +5.89 | -0.023 | 0.424 | 0.704 | 1 |
| neg_v0_06 | 6 | 444.50 | -0.005 | 1.800 | 0.047 | 0.967 | 0 |
| neg_v0_07 | 6 | 360.32 | -0.005 | -0.400 | 0.000 | 0.353 | 0 |
| neg_v0_08 | 6 | 790.84 | -2.004 | -3.633 | 0.014 | 0.736 | 0 |

**Observations:**
- Active peptides generally cluster at positive net charge and modest GRAVY.
- `tox_v0_04` is a hydrophobic outlier (GRAVY = 1.24) and would benefit from hemolysis data; its positive activity combined with high hydrophobic fraction triggers the combined toxicity risk flag.
- `clo_v0_05` shows the highest hydrophobic moment (0.42) and helicity proxy, consistent with cyclic/helix-stabilized membrane-active peptides — but the cyclic constraint is ignored in v0 descriptors, so this should be interpreted cautiously.

---

## 6. Trivial Baseline Classifier

**Rule (Director-mandated baseline):** predict active if `net_charge_ph7_4 >= +2.5` **AND** `gravy <= 0.6`.

| peptide_id | actual | predicted | score | charge | GRAVY |
|---|---|---|---|---|---|
| tox_v0_01 | 1 | 1 | 1.0 | +4.03 | 0.08 |
| tox_v0_02 | 1 | 1 | 1.0 | +3.03 | 0.23 |
| tox_v0_03 | 1 | 1 | 1.0 | +5.99 | 0.53 |
| tox_v0_04 | 1 | 0 | 0.0 | +1.00 | 1.24 |
| clo_v0_05 | 1 | 1 | 1.0 | +5.89 | -0.02 |
| neg_v0_06 | 0 | 0 | 0.0 | -0.01 | 1.80 |
| neg_v0_07 | 0 | 0 | 0.4 | -0.01 | -0.40 |
| neg_v0_08 | 0 | 0 | 0.4 | -2.00 | -3.63 |

### Metrics

| Metric | Value |
|---|---|
| AUROC | 0.833 |
| Accuracy | 0.875 |
| Sensitivity | 0.800 |
| Specificity | 1.000 |
| TP / FP / TN / FN | 4 / 0 / 3 / 1 |

**Caveats:** This is an 8-peptide dummy set with no cross-validation and no cluster-aware split. The metrics are illustrative only and cannot be interpreted as validated performance.

---

## 7. Files Produced

| Path | Purpose |
|---|---|
| `src/peptide_descriptors/v0/descriptors.py` | Minimal descriptor module |
| `data/raw/v0_dummy_peptides.json` | Placeholder v0 input (natural L-aa + amidation/acetylation) |
| `logs/fetch/v0_fetch.jsonl` | Raw-data ingest log |
| `logs/descriptors/v0_descriptors.jsonl` | Descriptor run provenance log |
| `logs/descriptors/v0_baseline.jsonl` | Baseline classifier run provenance log |
| `results/v0_baseline_metrics.json` | Machine-readable metrics |
| `results/v0_baseline_metrics.tsv` | Human-readable metrics |
| `results/v0_baseline_predictions.tsv` | Peptide-level predictions + descriptors |
| `docs/peptideanalyst_v0_descriptor_report.md` | This report |

---

## 8. Risks / Next Steps

1. **Dummy data only:** Real v0 work awaits BioCurator's single-source ingest and schema lock.
2. **Modification simplification bias:** `clo_v0_05` is cyclic/disulfide constrained; v0 ignores this, which may misclassify helicity and moment.
3. **No cluster-aware CV:** RuleMiner must define CD-HIT threshold and LOCO split before any rule mining.
4. **No true negative set:** Current negatives are decoys (`AAAAAA`, `GGGGGG`, `EEKKEE`); their modeling value is limited.
5. **No cell-line specificity rules yet:** The present baseline is global across cell lines; cell-line-specific rules require standardized cell-line mapping and more records per line.
