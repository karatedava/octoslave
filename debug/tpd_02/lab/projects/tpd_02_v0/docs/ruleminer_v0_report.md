# RuleMiner v0 Report — Cluster-Aware CV & Rule Success Metrics

**Owner:** RuleMiner  
**Project:** TPD-02 (Therapeutic Anticancer Peptide Explainability)  
**Date:** 2026-06-15  
**Schema version:** v0.1  

## 1. What was delivered
RuleMiner produced the v0 statistical-validation deliverables required by the Director synthesis:

1. **`docs/v0_cluster_cv_protocol.md`** — defines CD-HIT 70% clustering + Leave-One-Cluster-Out (LOCO) CV, with exact-identity fallback, tiny-cluster handling, and escalation fallbacks.
2. **`docs/v0_success_metrics.md`** — gating and secondary metrics for validated sequence/structural rules (LOCO AUROC ≥ 0.70, OR > 2.0, coverage ≥ 3 clusters, toxicity guard required). Includes a pass/partial/fail decision table.
3. **`src/ruleminer/v0/cluster_cv.py`** — LOCO split generator, LOCO AUROC/AUPRC aggregator, JSONL provenance logger.
4. **`src/ruleminer/v0/evaluate_rules.py`** — candidate rule evaluator with Fisher exact test, Odds Ratio, precision, recall, cluster coverage, Benjamini-Hochberg correction, toxicity-guard flag.
5. **`data/processed/v0_cluster_assignments.tsv`** — placeholder cluster assignments derived from the PeptideAnalyst dummy dataset (8 peptides, 7 clusters).
6. **`results/v0_loco_metrics.json`** — LOCO performance of the trivial baseline on the dummy data.
7. **`results/v0_rule_metrics.tsv`** — example rule evaluations on the dummy data.
8. **`logs/cv/v0_cluster_cv.jsonl`**, **`logs/rules/v0_rule_eval.jsonl`** — provenance logs honoring the lab logging contract.

## 2. Baseline LOCO evaluation on the dummy v0 dataset
Because the Director synthesis requires a **baseline before advanced rule mining**, we first ran the PeptideAnalyst trivial comparator inside the new LOCO framework.

| Metric | Value |
|--------|-------|
| n_total (evaluated peptides) | 8 |
| n_folds (clusters held out) | 7 |
| Prevalence (active) | 0.625 |
| **LOCO AUROC** | **0.8333** |
| LOCO AUPRC | 0.925 |
| AUPRC lift vs. prevalence | 1.48 |

**Interpretation:** The trivial charge + gravy rule already exceeds the v0 primary threshold of LOCO AUROC ≥ 0.70 on this tiny illustrative dataset. This is expected with only 8 peptides and hand-picked decoys; it is **not** evidence of generalizable performance. Many per-cluster folds are singletons, so no per-cluster AUROC was computable.

## 3. Candidate rule evaluation
Four candidate rules were evaluated on the same dummy data. Results are **illustrative only** because the dataset is far below the v0 minimum (n ≥ 50) and negatives are decoys.

| Rule | Conditions | TP | FP | FN | TN | Precision | Recall | OR | Coverage | Toxicity guard present? | BH p ≤ 0.05? |
|------|------------|----|----|----|----|-----------|--------|----|----------|------------------------|--------------|
| Charge + Gravy | net_charge ≥ 2.5 AND gravy ≤ 0.6 | 4 | 0 | 1 | 3 | 1.00 | 0.80 | ∞ | 3 | Yes* | No |
| High charge only | net_charge ≥ 4.0 | 3 | 0 | 2 | 3 | 1.00 | 0.60 | ∞ | 3 | Yes* | No |
| Helical amphipathic | hydrophobic_moment ≥ 0.25 AND helicity ≥ 0.6 AND tox_risk == 0 | 2 | 0 | 3 | 3 | 1.00 | 0.40 | ∞ | 2 | Yes | No |
| Charge + Gravy + Tox guard | net_charge ≥ 2.5 AND gravy ≤ 0.6 AND tox_risk == 0 | 1 | 0 | 4 | 3 | 1.00 | 0.20 | ∞ | 1 | Yes | No |

\* The first two rules do not explicitly reference `flag_combined_tox_risk` in the rule, but the evaluator attaches the toxicity-guard metadata column because the guard is available in the input. Per `v0_success_metrics.md`, a rule must **include** a toxicity guard to count as validated for in vivo transfer.

**Verdict on dummy data:** No rule is statistically significant after BH correction (all p > 0.05). This is exactly what should happen with n=8.

## 4. Readiness checklist for real v0 data
| Requirement | Status | Notes |
|-------------|--------|-------|
| Cluster-aware CV protocol defined | ✅ | `docs/v0_cluster_cv_protocol.md` |
| Success metrics defined | ✅ | `docs/v0_success_metrics.md` |
| LOCO code implemented and exercised | ✅ | `src/ruleminer/v0/cluster_cv.py` |
| Rule evaluator implemented and exercised | ✅ | `src/ruleminer/v0/evaluate_rules.py` |
| Baseline run inside LOCO | ✅ | AUROC 0.8333 on dummy data |
| Real curated dataset (n ≥ 50, one endpoint) | ⏳ | Blocked on BioCurator v0 ingest |
| True experimental negative set | ⏳ | Blocked on BioCurator |
| v0 success verdict | ⏳ | Cannot be declared on placeholder data |

## 5. How RuleMiner will proceed once real data arrive
1. BioCurator provides `data/processed/v0_curated.tsv` with `canonical_sequence`, `label_active`, `endpoint_type`, `cell_line`, `source_db`.
2. RuleMiner supplies that to CD-HIT 70% (or the Python fallback) and writes `data/processed/v0_cluster_assignments.tsv`.
3. PeptideAnalyst descriptors are merged into the cluster table.
4. RuleMiner re-runs LOCO baseline, then mines/search candidate rules and evaluates them against `v0_success_metrics.md`.
5. Results feed into the ReportEngineer validation report and, if metrics pass, the first feature-RULES section.

## 6. Risks flagged for the team
- **Dummy data overfit risk:** The baseline AUROC of 0.83 on 8 peptides is meaningless for real-world performance. It is provided only to exercise the pipeline.
- **Decoy-only negatives:** Current negatives are shuffled/decoy sequences. True experimental inactives are preferred; otherwise all potency metrics must be labeled "decoy-dependent".
- **Cluster singletons:** With few clusters and small sizes, LOCO variance will be high. The protocol therefore sets a minimum dataset size (n ≥ 50, ≥ 5 clusters) before declaring success.
- **CD-HIT availability:** The protocol assumes CD-HIT can be installed; an exact Python fallback is already coded if needed.
