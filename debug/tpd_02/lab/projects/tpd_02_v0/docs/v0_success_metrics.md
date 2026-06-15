# v0 Success Metrics for Anticancer Peptide Rules

**Owner:** RuleMiner  
**Scope:** TPD-02 v0 proof-of-concept — sequence/structural rules predicting potent, low-toxicity anticancer peptides that generalize to held-out sequence clusters and can guide in vivo design.  
**Schema version:** v0.1  
**Date:** 2026-06-15  

## 1. Goal
Define a minimal, testable success criterion for v0 rule mining. Metrics are split into three tiers:
1. **Prerequisite (must pass before any rule report)** — valid CV setup and baseline.
2. **Primary (gating success of v0)** — cluster-aware predictive performance and trustworthy rule discovery.
3. **Secondary (nice-to-have)** — in vivo/toxicity relevance, interpretability.

Only primary metrics determine whether v0 is allowed to proceed to v1 scaling.

---

## 2. Prerequisite metrics
### 2.1 Dataset readiness
| Metric | Requirement | Why |
|--------|-------------|-----|
| `n_total_peptides` | `>= 50` | Rule mining with fewer than ~50 independent peptides is under-powered. |
| `n_active_peptides` | `>= 25` | Need enough positives to discover patterns with confident effect size. |
| `n_clusters_70pct` | `>= 5` | LOCO CV needs at least 5 held-out clusters for a stable mean AUROC. |
| `max_cluster_size` / `n_total` | `<= 0.5` | No single cluster should dominate >50% of the data. |
| `negative_type` documented | present in metadata | Each inactive must be marked `experimental` or `decoy/shuffled`. |

### 2.2 Baseline comparator (charge + gravy threshold)
The PeptideAnalyst trivial comparator is run **inside the same LOCO CV** as a mandatory floor.

| Metric | Requirement |
|--------|-------------|
| `baseline_loco_auroc` | reported with 95% CI if `n_clusters >= 10` |
| `baseline_improved_by_rules` | The best rule/rule-set must exceed baseline AUROC on this dataset. |

**Gate:** If baseline AUROC is already `>= 0.90`, advanced rule mining is de-prioritized in favor of reporting the baseline transparently and pursuing mechanistic refinement in v1.

---

## 3. Primary success metrics (gating)
All primary metrics are measured by **LOCO cluster CV** on the v0 dataset.

### 3.1 Predictive performance
| Metric | Target | Justification |
|--------|--------|---------------|
| **LOCO AUROC** | `>= 0.70` | Minimum credible discrimination for a design prioritization tool. |
| **LOCO sensitivity (recall)** | `>= 0.60` | Must capture most true actives to be useful for screening. |
| **LOCO specificity** | `>= 0.60` | Must avoid too many false positives for expensive assays. |
| **LOCO AUPRC vs. prevalence** | `>= 1.5 × prevalence` | Better than random guessing, important if class imbalance exists. |

### 3.2 Rule quality
A **validated rule** is a human-readable conjunction/disjunction of v0 descriptors (e.g., `charge >= +2.5 AND gravy <= 0.6 AND helicity_proxy >= 0.55`) that:

| Metric | Target | Justification |
|--------|--------|---------------|
| **Odds Ratio (OR)** | `> 2.0` | Rule must at least double the odds of activity vs. its complement. |
| **Coverage** | `>= 3 clusters` | Rule must fire in at least 3 distinct sequence clusters in the held-out aggregate. |
| **Precision on held-out rule-positive set** | `>= 0.60` | When rule fires, at least 60% of those peptides are active. |
| **FDR-corrected p-value** | `<= 0.05` (Benjamini–Hochberg across rule search space) | Credible association, not cherry-picked. |

### 3.3 Number of validated rules
- **Minimum:** at least **2 validated rules** meeting the criteria above.
- **Maximum for v0:** cap at **8 rules** to avoid overfitting and to keep the report interpretable.

### 3.4 Low-toxicity coupling
Because the human brief explicitly asks for "low toxicity risk", each validated rule must include at least one toxicity-related guard from the v0 descriptor module, e.g.:
- `net_charge_ph7_4 < +5` (extreme positive charge correlates with hemolysis).
- `flag_combined_tox_risk == 0` (no high charge + high hydrophobicity + high hydrophobic moment).
- `hydrophobic_moment <= 0.45`.

Failure to couple any rule to a toxicity guard means v0 is **partially successful** for potency but **not approved for in vivo transfer** claims.

---

## 4. Secondary metrics (interpretability / in vivo translation)
These are reported but do **not** gate v0 success.

| Metric | Target / Report |
|--------|-----------------|
| **Rule stability** | 80% of top rules reappear when CD-HIT threshold varied 60%–80%. |
| **In vivo evidence coverage** | % of rule-positive peptides with any mouse/in vivo data (report only). |
| **Cell-line specificity** | Per-cell-line LOCO AUROC if `n_cell_lines >= 5`. |
| **Rule sparsity** | Average number of conditions per rule <= 4 (prefer simpler rules). |
| **Rule actionability** | Each rule maps to a concrete design recommendation (e.g., "include 2+ Arg/Lys and keep mean hydropathy moderate"). |

---

## 5. Red-flags that block v0 approval
Even if some metrics meet targets, the following issues require remediation before declaring success:

1. **Leaky CV** — any information from the held-out cluster used during descriptor normalization, threshold selection, or rule search.
2. **Single-cluster rules** — all validated rules fire only within one CD-HIT 70% cluster.
3. **Decoy-only negatives** — if no experimental inactive peptides exist, all potency AUROCs must be reported as "preliminary / decoy-dependent".
4. **Contradictory physicochemistry** — rules that recommend both very high charge and very high hydrophobicity without a toxicity guard (this is the classic hemolysis liability pattern).
5. **Unreproducible provenance** — missing or inconsistent JSONL fetch/descriptor/CV logs (`lab/projects/tpd_02_v0/logs/`).

---

## 6. Decision table
| LOCO AUROC | Validated rules OR>2, ≥3 clusters | Toxicity guard present | Verdict |
|------------|-----------------------------------|------------------------|---------|
| < 0.70     | —                                 | —                      | **FAIL**: improve data/model before v1. |
| >= 0.70    | < 2                               | —                      | **PARTIAL**: predictive but not yet explainable; revisit descriptors or rule search. |
| >= 0.70    | >= 2                              | no                     | **PARTIAL/CONDITIONAL**: potent rules exist, but in vivo/low-toxicity claims are unsupported. |
| >= 0.70    | >= 2                              | yes                    | **PASS**: proceed to v1 (more sources/endpoints/modifications). |

---

## 7. Outputs
| File | Purpose |
|------|---------|
| `docs/v0_success_metrics.md` | This document. |
| `src/ruleminer/v0/evaluate_rules.py` | Computes all metrics from predictions + labels + cluster IDs. |
| `results/v0_rule_metrics.tsv` | One row per candidate rule with OR, coverage, precision, p-value, toxicity guard flag. |
| `results/v0_loco_metrics.tsv` | Aggregated LOCO performance vs. baseline. |
| `logs/rules/v0_rule_eval.jsonl` | Provenance log for every rule evaluation run. |
| `logs/cv/v0_cluster_cv.jsonl` | Provenance log for CV runs. |

---

## 8. Relationship to in vivo transfer
The v0 dataset may not contain mouse data. Success metrics therefore treat **"transferable to in vivo testing"** as a *design constraint* (low-toxicity, physicochemically plausible rules) rather than a *validated outcome*. Claims of in vivo transfer require:
- At least one validated rule whose positive-set peptides are enriched for in-vivo-confirmed actives (v1), OR
- Explicit statement that in vivo transfer is an untested computational hypothesis requiring experimental follow-up.
