# v0 Cluster-Aware Cross-Validation Protocol

**Owner:** RuleMiner  
**Scope:** TPD-02 v0 proof-of-concept — natural L-amino acids, binary amidation/acetylation flags, one endpoint type.  
**Schema version:** v0.1  
**Date:** 2026-06-15  

## 1. Goal
Ensure that any sequence/structural rule mined from the v0 dataset is evaluated for **hold-out-cluster generalization**, i.e. it must work on peptide clusters that were not used during rule discovery or model selection. This protects against overfitting to particular sequence families and gives a fairer estimate of transfer to new sequences and — by extension — to in vivo contexts.

## 2. Why sequence-clustered CV matters for ACPs
Anticancer peptide datasets are typically dominated by a few protein families (e.g., cecropin/magainin/histone-derived fragments, cathelicidins, defensins). A model that memorizes family-specific residues can score well during standard random-fold CV but fail completely on a structurally unrelated peptide. Sequence-similarity clustering approximates "family" boundaries and forces the validation to estimate performance on genuinely new sequences.

## 3. Clustering step: CD-HIT at 70% global identity
We cluster peptide sequences **before** any split using **CD-HIT** (or an equivalent exact implementation) at **70% global sequence identity**.

### 3.1 Choice of threshold
- **70% identity** is a conservative starting point for short peptides. It is stricter than the 80–90% commonly used for full-length proteins and prevents very similar analogues from leaking between training and test folds.
- **Rationale for v0:** Start strict. If the dataset is small and clusters are too fragmented to support Leave-One-Cluster-Out (LOCO), we can relax to 60% as a documented fallback (see §6).
- Redundant analogues with single-residue substitutions (common in ACP SAR papers) will usually fall into the same cluster and be kept together in one fold.

### 3.2 CD-HIT settings
```
cd-hit -i v0_sequences.fasta -o v0_clusters_70 -c 0.70 -n 4 -s 0.8 -aL 0.5 -d 0
```
Parameter notes:
- `-c 0.70` — identity threshold 70%.
- `-n 4` — word size appropriate for the threshold.
- `-s 0.8` — length difference cutoff 80% (short peptides can vary in length, so this is lenient).
- `-aL 0.5` — alignment coverage on the longer sequence 50%, chosen because peptides are short.
- `-d 0` — suppress sequence name length restriction.

**Fallback if CD-HIT is unavailable:** A pure-Python exact implementation using `biopython` or a simple pairwise global-identity pass will be used and documented in `src/ruleminer/v0/clustering.py`.

### 3.3 Cluster naming and record
Each peptide gets a `cluster_id` assigned by the CD-HIT `.clstr` output. The clustering result is written to `data/processed/v0_cluster_assignments.tsv` with columns:
- `peptide_id`
- `canonical_sequence`
- `cluster_id`
- `cluster_representative` (1/0)
- `n_members_in_cluster`

## 4. Splitting strategy: Leave-One-Cluster-Out (LOCO)
Because clusters are unequal in size, **k-fold stratification on clusters is unstable**. The preferred v0 protocol is **Leave-One-Cluster-Out (LOCO)**:

- For each cluster `C_i`, train on all peptides **not** in `C_i` and test on `C_i`.
- Aggregate predictions across all held-out clusters to compute one AUROC, one set of rule detection rates, etc.
- Repeat with a **random-cluster shuffle** sensitivity check (shuffle cluster labels, rerun LOCO; expect AUROC near 0.5 for the shuffle).

### 4.1 Handling tiny clusters
A cluster with **fewer than 2 peptides** cannot give a meaningful per-cluster metric. Those clusters are still held out, but their predictions are pooled with the global LOCO aggregate rather than reported individually.

### 4.2 Target leakage guard
- All descriptor computation and rule mining must happen **inside the training split** only. Test-set descriptors can be computed independently, but any thresholds/rules are learned on the training data.
- If the dataset contains multiple measurements of the same peptide on different cell lines, we ensure the peptide (by sequence) stays together in one cluster; cell-line-specific models are evaluated with **Leave-Cell-Line-Out** as a secondary protocol (§7), not the primary v0 metric.

## 5. Baseline comparator (required before advanced rule mining)
As mandated by the Director synthesis, RuleMiner will first evaluate the PeptideAnalyst trivial baseline:
```
active if (net_charge_ph7_4 >= +2.5) AND (gravy <= 0.6)
```
on the LOCO split. This gives the AUROC floor against which any mined rule must improve.

## 6. Fallbacks and escalation path
| Situation | Action |
|-----------|--------|
| CD-HIT not installed | Use Python exact identity clustering; log fallback. |
| Too few clusters for LOCO (`n_clusters < 5`) | Relax CD-HIT to 60% identity to increase granularity; document rationale. |
| Still too few clusters | Switch to stratified Monte Carlo split identity split: sample so that no pair >70% identity appears in both train and test, 5 repeats. |
| Negative set entirely decoys | Add a column `negative_type` (`experimental`/`decoy`) and report metrics with/without decoys. |

## 7. Secondary protocols (after baseline)
1. **Leave-Cell-Line-Out (LCO)** — exercise cell-line-specific generalization if `n_cell_lines >= 5`.
2. **Leave-One-Source-Out (LOSO)** — if multiple databases are combined, assess source bias.
3. **Stratified random 5-fold** — reported only for comparison; not the primary v0 metric.

## 8. Outputs
| File | Purpose |
|------|---------|
| `src/ruleminer/v0/cluster_cv.py` | Python module implementing LOCO splitting given a cluster assignment table. |
| `data/processed/v0_cluster_assignments.tsv` | Cluster IDs per peptide. |
| `docs/v0_cluster_cv_protocol.md` | This protocol. |
| `logs/cv/v0_cluster_cv.jsonl` | Per-run provenance: input hash, cluster threshold, n_clusters, split type, AUROC, timestamp, git commit. |
| `results/v0_loco_metrics.tsv` | Aggregated LOCO AUROC, baseline AUROC, sensitivity, specificity. Used by `v0_success_metrics.md`. |

## 9. Open risks
- **Cluster threshold is arbitrary.** We selected 70% to be conservative for short peptides; validation will need sensitivity analysis in v1.
- **Small cluster sizes** may make LOCO variance high; we report bootstrapped 95% CIs if `n_clusters >= 10`.
- **Sequence-only clustering ignores cell-line specificity.** A peptide may be active on one line and inactive on another; v0 restricts to one endpoint type per source and collapses labels to active/inactive for the main model. Cell-line stratification is a follow-up.
