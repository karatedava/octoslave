"""
Minimal v0 rule evaluation module.

Given a dataframe of peptides with descriptors, labels, and cluster IDs, this
module evaluates candidate IF-THEN rules of the form:
    IF (cond1 AND cond2 AND ...) THEN active

Each rule is scored by:
  - Odds Ratio
  - Coverage (number of distinct clusters where the rule fires)
  - Precision on the rule-positive set
  - Benjamini-Hochberg adjusted p-value (Fisher's exact vs. complement)
  - Toxicity-guard inclusion flag
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import scipy.stats as stats


WORKDIR = Path(__file__).resolve().parents[4]
LOG_RULES = WORKDIR / "logs" / "rules"
STD_AA = set("ACDEFGHIKLMNPQRSTVWY")


class RuleEvalError(ValueError):
    pass


def _git_commit() -> Optional[str]:
    try:
        import subprocess
        return (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=WORKDIR)
            .decode()
            .strip()
        )
    except Exception:
        return None


def _log_run(log_file: Path, record: Dict) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def evaluate_rule(
    df: pd.DataFrame,
    conditions: List[Tuple[str, str, float]],
    label_col: str = "label_active",
    cluster_col: str = "cluster_id",
    toxicity_guard_cols: Optional[List[str]] = None,
) -> Dict:
    """
    Evaluate one rule.

    conditions: list of (descriptor_col, op, threshold)
      op in {'>=', '<=', '>', '<', '=='}.
    """
    mask = pd.Series(True, index=df.index)
    for col, op, thr in conditions:
        if op == ">=":
            mask &= df[col] >= thr
        elif op == "<=":
            mask &= df[col] <= thr
        elif op == ">":
            mask &= df[col] > thr
        elif op == "<":
            mask &= df[col] < thr
        elif op == "==":
            mask &= df[col] == thr
        else:
            raise RuleEvalError(f"unsupported operator: {op}")

    pos = df.loc[mask]
    neg = df.loc[~mask]

    tp = int((pos[label_col] == 1).sum())
    fp = int((pos[label_col] == 0).sum())
    fn = int((neg[label_col] == 1).sum())
    tn = int((neg[label_col] == 0).sum())

    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    specificity = tn / max(tn + fp, 1)

    # Odds ratio with Haldane correction
    table = [[tp, fp], [fn, tn]]
    or_value, pvalue = stats.fisher_exact(
        [[tp + 0.5, fp + 0.5], [fn + 0.5, tn + 0.5]], alternative="greater"
    )

    coverage = int(pos[cluster_col].nunique()) if cluster_col in df.columns else len(pos)

    tox_guard_present = False
    if toxicity_guard_cols:
        tox_guard_present = all(c in df.columns for c in toxicity_guard_cols)

    rule_str = " AND ".join(f"{c} {op} {thr}" for c, op, thr in conditions)

    return {
        "rule": rule_str,
        "n_conditions": len(conditions),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "support": int(mask.sum()),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "specificity": round(specificity, 4),
        "odds_ratio": round(or_value, 4),
        "fisher_pvalue": pvalue,
        "coverage_clusters": coverage,
        "toxicity_guard_columns": toxicity_guard_cols,
        "toxicity_guard_present": tox_guard_present,
    }


def benjamini_hochberg(rules: List[Dict], alpha: float = 0.05) -> List[Dict]:
    """Add BH-adjusted p-values and discovery flag to each rule dict."""
    m = len(rules)
    if m == 0:
        return rules
    # sort ascending p
    ordered = sorted(rules, key=lambda x: x["fisher_pvalue"])
    for i, r in enumerate(ordered, start=1):
        r["bh_pvalue"] = min(r["fisher_pvalue"] * m / i, 1.0)
    for r in ordered:
        r["is_significant"] = r["bh_pvalue"] <= alpha
    return ordered


def evaluate_candidate_rules(
    input_tsv: Path,
    rules: List[List[Tuple[str, str, float]]],
    output_tsv: Optional[Path] = None,
    log_file: Optional[Path] = None,
    toxicity_guard_cols: Optional[List[str]] = None,
    alpha: float = 0.05,
    agent: str = "RuleMiner",
    schema_version: str = "v0.1",
) -> List[Dict]:
    """
    Evaluate a list of candidate rules and write a TSV + JSONL provenance.
    """
    df = pd.read_table(input_tsv)
    results = [evaluate_rule(df, r, toxicity_guard_cols=toxicity_guard_cols) for r in rules]
    results = benjamini_hochberg(results, alpha=alpha)

    if output_tsv:
        output_tsv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(results).to_csv(output_tsv, sep="\t", index=False)

    if log_file:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "tool": "evaluate_rules.evaluate_candidate_rules",
            "source_url_doi": str(input_tsv),
            "input_hash": hashlib.sha256(open(input_tsv, "rb").read()).hexdigest(),
            "output_file": str(output_tsv) if output_tsv else None,
            "schema_version": schema_version,
            "git_commit": _git_commit(),
            "n_rules_evaluated": len(results),
            "n_rules_significant": int(sum(r["is_significant"] for r in results)),
        }
        _log_run(log_file, record)

    return results
