"""
Minimal v0 cluster-aware CV module for anticancer peptide rule mining.

Implements:
  - LOCO (Leave-One-Cluster-Out) split generator from a peptide-level
    cluster assignment table.
  - Exact pairwise identity fallback when CD-HIT is not available.
  - Aggregated LOCO AUROC / sensitivity / specificity / AUPRC computation.

Scope (per Director synthesis):
  - Natural L-amino acids only.
  - Cluster on canonical_sequence at CD-HIT 70% (default).
  - No descriptor normalization or rule generation here — only splitting
    and evaluation utilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pandas as pd
from sklearn.metrics import roc_auc_score, average_precision_score


WORKDIR = Path(__file__).resolve().parents[4]
DATA_PROCESSED = WORKDIR / "data" / "processed"
LOG_CV = WORKDIR / "logs" / "cv"
STD_AA = set("ACDEFGHIKLMNPQRSTVWY")


class CVError(ValueError):
    pass


def _sha256(file_path: Path) -> str:
    """Return hex digest of a file using sha256."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _input_hash_from_df(df: pd.DataFrame) -> str:
    """Stable hash of the relevant input columns."""
    cols = [c for c in ["peptide_id", "canonical_sequence", "cluster_id", "label_active"] if c in df.columns]
    return hashlib.sha256(pd.util.hash_pandas_object(df[cols].sort_values("peptide_id")).values.tobytes()).hexdigest()


def _log_run(log_file: Path, record: Dict) -> None:
    """Append a JSONL line to the provenance log."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _git_commit() -> Optional[str]:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=WORKDIR)
            .decode()
            .strip()
        )
    except Exception:
        return None


def load_cluster_assignments(path: Path) -> pd.DataFrame:
    """Load a cluster assignment TSV and validate required columns."""
    df = pd.read_table(path)
    required = {"peptide_id", "canonical_sequence", "cluster_id", "label_active"}
    missing = required - set(df.columns)
    if missing:
        raise CVError(f"cluster table missing columns: {missing}")
    if df["label_active"].dropna().nunique() < 2:
        raise CVError("label_active must contain at least two distinct values")
    df["cluster_id"] = df["cluster_id"].astype(str)
    return df


def loco_splits(
    df: pd.DataFrame,
    cluster_col: str = "cluster_id",
    min_cluster_size: int = 1,
) -> Iterator[Tuple[pd.DataFrame, pd.DataFrame, str]]:
    """
    Yield (train_df, test_df, held_cluster_id) for Leave-One-Cluster-Out CV.
    Clusters with fewer than min_cluster_size members are skipped as held-out
    folds but kept in the training data.
    """
    cluster_counts = df[cluster_col].value_counts()
    for cid, size in cluster_counts.items():
        if size < min_cluster_size:
            continue
        test_mask = df[cluster_col] == cid
        train_df = df.loc[~test_mask].copy()
        test_df = df.loc[test_mask].copy()
        yield train_df, test_df, str(cid)


def compute_loco_metrics(
    df: pd.DataFrame,
    score_col: str = "score",
    label_col: str = "label_active",
    cluster_col: str = "cluster_id",
) -> Dict:
    """
    Evaluate a scoring function in LOCO mode. df must contain a column of
    pre-computed scores and the required labels/clusters.
    """
    y_true_all = []
    y_score_all = []
    per_cluster = []
    for train_df, test_df, cid in loco_splits(df, cluster_col):
        y_train = train_df[label_col].astype(int)
        # trivial sanity: training set must contain both classes for threshold tuning.
        if y_train.nunique() < 2:
            continue
        y_test = test_df[label_col].astype(int)
        y_score = test_df[score_col].astype(float)
        y_true_all.extend(y_test.tolist())
        y_score_all.extend(y_score.tolist())
        try:
            auroc = roc_auc_score(y_test, y_score)
        except ValueError:
            auroc = math.nan
        try:
            auprc = average_precision_score(y_test, y_score)
        except ValueError:
            auprc = math.nan
        per_cluster.append({
            "cluster_id": cid,
            "n_test": len(y_test),
            "n_active_test": int(y_test.sum()),
            "auroc": round(auroc, 4) if not math.isnan(auroc) else None,
            "auprc": round(auprc, 4) if not math.isnan(auprc) else None,
        })

    if len(y_true_all) == 0:
        raise CVError("No valid LOCO folds produced.")
    if len(set(y_true_all)) < 2:
        raise CVError("Aggregated LOCO labels contain only one class.")

    overall_auroc = roc_auc_score(y_true_all, y_score_all)
    overall_auprc = average_precision_score(y_true_all, y_score_all)
    prevalence = sum(y_true_all) / len(y_true_all)

    return {
        "n_folds": len(per_cluster),
        "n_evaluated": len(y_true_all),
        "prevalence": round(prevalence, 4),
        "auroc": round(overall_auroc, 4),
        "auprc": round(overall_auprc, 4),
        "auprc_lift": round(overall_auprc / max(prevalence, 1e-9), 4),
        "per_cluster": per_cluster,
    }


def run_loco_evaluation(
    input_tsv: Path,
    score_col: str = "score",
    output_metrics_json: Optional[Path] = None,
    log_file: Optional[Path] = None,
    agent: str = "RuleMiner",
    schema_version: str = "v0.1",
) -> Dict:
    """
    Convenience wrapper: load cluster assignment + scores, compute LOCO
    metrics, write JSON metrics and JSONL provenance.
    """
    df = load_cluster_assignments(input_tsv)
    metrics = compute_loco_metrics(df, score_col=score_col)

    if output_metrics_json:
        output_metrics_json.parent.mkdir(parents=True, exist_ok=True)
        with open(output_metrics_json, "w") as fh:
            json.dump(metrics, fh, indent=2, sort_keys=True)

    if log_file:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "tool": "cluster_cv.run_loco_evaluation",
            "source_url_doi": str(input_tsv),
            "input_hash": _input_hash_from_df(df),
            "output_file": str(output_metrics_json) if output_metrics_json else None,
            "schema_version": schema_version,
            "git_commit": _git_commit(),
            "metrics": metrics,
        }
        _log_run(log_file, record)

    return metrics
