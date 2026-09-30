"""Offline, release-gated temporal experiments. No runtime model loading."""

import json
import math
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.risk.dataset import FEATURES, TARGET, VERSION
from app.services.normalization import fingerprint

EMBARGO_DAYS = 30


def timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Timezone-aware availability required")
    return stamp


def valid_row(row: dict[str, Any], as_of: datetime) -> bool:
    try:
        cutoff = timestamp(row["cutoff"])
        available = timestamp(row["label_available_at"])
        start = datetime.fromisoformat(row["label_start"]).date()
        end = datetime.fromisoformat(row["label_end"]).date()
        if not (cutoff.date() < start <= end < available.date() and available <= as_of):
            return False
        if not 330 <= (end - start).days + 1 <= 385 or row["label"] not in {0, 1}:
            return False
        if set(row["features"]) != set(FEATURES):
            return False
        if any(
            v is not None and (isinstance(v, bool) or not math.isfinite(float(v)))
            for v in row["features"].values()
        ):
            return False
        sources = row["feature_sources"]
        if not sources or not row["label_source"]["source"]["fact_id"]:
            return False
        for source in sources.values():
            if source["value"] is not None:
                if (
                    timestamp(str(source["source"]["first_observed_at"])) > cutoff
                    or timestamp(str(source["reported_available_at"])) > cutoff
                    or not source["source"]["fact_id"]
                ):
                    return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def temporal_split(
    rows: list[dict[str, Any]], validation: datetime, test: datetime
) -> dict[str, list[dict[str, Any]]]:
    if validation >= test:
        raise ValueError("Validation must precede held-out test")
    splits: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    for row in rows:
        cutoff, available = timestamp(row["cutoff"]), timestamp(row["label_available_at"])
        if cutoff < validation:
            if available <= validation - timedelta(days=EMBARGO_DAYS):
                splits["train"].append(row)
        elif cutoff < test:
            if available <= test - timedelta(days=EMBARGO_DAYS):
                splits["validation"].append(row)
        else:
            splits["test"].append(row)
    return splits


def gate(
    dataset: dict[str, Any], validation: datetime | None = None, test: datetime | None = None
) -> dict[str, Any]:
    reasons = []
    rows = dataset.get("rows", [])
    if dataset.get("version") != VERSION or dataset.get("target") != TARGET:
        reasons.append("incompatible_dataset_definition")
    if list(dataset.get("feature_order", [])) != list(FEATURES):
        reasons.append("feature_order_mismatch")
    if len(rows) < 300:
        reasons.append("fewer_than_300_labelled_issuer_years")
    if len({r["cik"] for r in rows}) < 50:
        reasons.append("fewer_than_50_labelled_issuers")
    if not dataset.get("cohort", {}).get("historical_inactive_coverage_verified"):
        reasons.append("historical_inactive_cohort_coverage_unverified")
    as_of = timestamp(dataset["as_of"])
    if any(not valid_row(r, as_of) for r in rows):
        reasons.append("invalid_lineage_or_temporal_leakage")
    if len({(r["cik"], r["label_start"]) for r in rows}) != len(rows):
        reasons.append("duplicate_issuer_outcomes")
    counts = Counter(r["label"] for r in rows)
    if min(counts.get(0, 0), counts.get(1, 0)) < 60:
        reasons.append("insufficient_positive_or_negative_outcomes")
    missingness = {
        f: sum(r["features"].get(f) is None for r in rows) / len(rows) if rows else None
        for f in FEATURES
    }
    if any(v is not None and v > 0.5 for v in missingness.values()):
        reasons.append("feature_missingness_exceeds_half")
    split_counts = {}
    if validation is None or test is None:
        reasons.append("explicit_temporal_split_boundaries_required")
    else:
        splits = temporal_split(rows, validation, test)
        for name, selected in splits.items():
            classes = Counter(r["label"] for r in selected)
            split_counts[name] = {
                "rows": len(selected),
                "issuers": len({r["cik"] for r in selected}),
                "classes": dict(classes),
            }
            if min(classes.get(0, 0), classes.get(1, 0)) < 20:
                reasons.append(f"{name}_has_fewer_than_20_examples_per_class")
    return {
        "status": "READY_FOR_OFFLINE_EXPERIMENT" if not reasons else "BLOCKED_BY_DATA",
        "reasons": reasons,
        "labelled_rows": len(rows),
        "class_counts": dict(counts),
        "missingness": missingness,
        "split_counts": split_counts,
        "embargo_days": EMBARGO_DAYS,
        "inference_available": False,
        "thresholds": "Engineering minimums, not guarantees of statistical adequacy",
    }


def train(
    dataset: dict[str, Any], output: Path, validation: datetime, test: datetime
) -> dict[str, Any]:
    admission = gate(dataset, validation, test)
    if admission["status"] != "READY_FOR_OFFLINE_EXPERIMENT":
        return admission
    # Imports and fitting happen only after the data gate. No live endpoint trains a model.
    import numpy as np
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from xgboost import XGBClassifier

    def arrays(rows: list[dict[str, Any]]) -> tuple[Any, Any]:
        return (
            np.array(
                [
                    [r["features"][f] if r["features"][f] is not None else np.nan for f in FEATURES]
                    for r in rows
                ]
            ),
            np.array([r["label"] for r in rows]),
        )

    def metrics(y: Any, p: Any, threshold: float) -> dict[str, Any]:
        pred = p >= threshold
        return {
            "pr_auc_average_precision": float(average_precision_score(y, p)),
            "roc_auc": float(roc_auc_score(y, p)),
            "brier": float(brier_score_loss(y, p)),
            "precision": float(precision_score(y, pred, zero_division=0)),
            "recall": float(recall_score(y, pred, zero_division=0)),
            "f1": float(f1_score(y, pred, zero_division=0)),
            "confusion_matrix": confusion_matrix(y, pred, labels=[0, 1]).tolist(),
        }

    splits = temporal_split(dataset["rows"], validation, test)
    x, y = arrays(splits["train"])
    vx, vy = arrays(splits["validation"])
    tx, ty = arrays(splits["test"])
    # Exclusive creation prevents re-evaluating held-out labels in this experiment directory.
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "dataset_hash": dataset["dataset_hash"],
        "feature_order": FEATURES,
        "target": TARGET,
        "approved": False,
        "inference_available": False,
        "validation_start": validation.isoformat(),
        "test_start": test.isoformat(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    preprocess = make_pipeline(
        SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler()
    )
    x = preprocess.fit_transform(x)
    vx, tx = preprocess.transform(vx), preprocess.transform(tx)
    logistic = LogisticRegression(max_iter=2000, random_state=17).fit(x, y)
    candidates = []
    for depth in [2, 3]:
        candidate = XGBClassifier(
            n_estimators=150,
            max_depth=depth,
            learning_rate=0.05,
            random_state=17,
            n_jobs=1,
            eval_metric="logloss",
        )
        candidate.fit(x, y)
        probability = candidate.predict_proba(vx)[:, 1]
        candidates.append((float(average_precision_score(vy, probability)), candidate))
    model = max(candidates, key=lambda pair: pair[0])[1]
    # Sigmoid calibration and threshold selection use validation only.
    calibration = LogisticRegression(random_state=17).fit(
        model.predict(vx, output_margin=True).reshape(-1, 1), vy
    )
    val_probability = calibration.predict_proba(
        model.predict(vx, output_margin=True).reshape(-1, 1)
    )[:, 1]
    threshold = max(
        [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
        key=lambda t: f1_score(vy, val_probability >= t),
    )
    prediction = calibration.predict_proba(model.predict(tx, output_margin=True).reshape(-1, 1))[
        :, 1
    ]
    report = {
        "gate": admission,
        "threshold": threshold,
        "test": {
            "prevalence": metrics(ty, np.full(len(ty), y.mean()), 0.5),
            "logistic": metrics(ty, logistic.predict_proba(tx)[:, 1], 0.5),
            "xgboost": metrics(ty, prediction, threshold),
        },
        "release_status": "UNAPPROVED_RESEARCH_ARTIFACT",
        "release_blockers": [
            "walk_forward_and_issuer_disjoint_review_required",
            "uncertainty_and_subgroup_review_required",
        ],
        "test_issuers": len({r["cik"] for r in splits["test"]}),
        "test_prevalence": float(ty.mean()),
        "inference_available": False,
    }
    model.save_model(output / "model.ubj")
    parameters = {
        "medians": preprocess[0].statistics_.tolist(),
        "means": preprocess[1].mean_.tolist(),
        "scales": preprocess[1].scale_.tolist(),
        "calibration_coef": calibration.coef_.tolist(),
        "calibration_intercept": calibration.intercept_.tolist(),
        "threshold": threshold,
    }
    (output / "preprocessing.json").write_text(json.dumps(parameters, indent=2), encoding="utf-8")
    (output / "evaluation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output / "model-card.md").write_text(
        "# Unapproved cash-flow research model\n\nTarget: "
        + TARGET
        + "\n\nNot bankruptcy probability or investment advice. Release is blocked pending temporal, issuer-disjoint, subgroup and uncertainty review. No inference artifact is approved.\n",
        encoding="utf-8",
    )
    manifest["checksums"] = {
        p.name: __import__("hashlib").sha256(p.read_bytes()).hexdigest()
        for p in output.iterdir()
        if p.name != "manifest.json"
    }
    manifest["manifest_hash"] = fingerprint(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return report
