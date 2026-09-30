"""Offline, release-gated temporal experiments. No runtime model loading."""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

from app.risk.dataset import FEATURES, TARGET, VERSION
from app.services.normalization import fingerprint

EMBARGO_DAYS = 30


def explain_predictions(
    model: Any,
    transformed: Any,
    rows: list[dict[str, Any]],
    calibration: Any,
    threshold: float,
) -> list[dict[str, Any]]:
    """Exact TreeSHAP for the margin, linearly mapped to calibrated log-odds."""
    import numpy as np
    from xgboost import DMatrix

    values = np.asarray(transformed)
    if values.shape != (len(rows), len(FEATURES)) or not np.isfinite(values).all():
        raise ValueError("Invalid transformed feature matrix")
    if not rows or not 0 < threshold < 1:
        raise ValueError("Rows and a valid decision threshold are required")
    if any(set(row["features"]) != set(FEATURES) for row in rows):
        raise ValueError("Feature schema mismatch")
    if (
        np.shape(calibration.coef_) != (1, 1)
        or np.shape(calibration.intercept_) != (1,)
        or list(calibration.classes_) != [0, 1]
    ):
        raise ValueError("Expected binary sigmoid calibration")
    slope, intercept = float(calibration.coef_[0, 0]), float(calibration.intercept_[0])
    if not math.isfinite(slope) or not math.isfinite(intercept):
        raise ValueError("Invalid calibration parameters")
    booster = model.get_booster()
    matrix = DMatrix(values, feature_names=booster.feature_names)
    contributions = booster.predict(matrix, pred_contribs=True, approx_contribs=False)
    margins = booster.predict(matrix, output_margin=True)
    if contributions.shape != (len(rows), len(FEATURES) + 1):
        raise ValueError("Expected binary feature contributions")
    if not np.isfinite(contributions).all() or not np.isfinite(margins).all():
        raise ValueError("Non-finite model output")
    if not np.allclose(contributions.sum(axis=1), margins, rtol=1e-5, atol=1e-5):
        raise ValueError("SHAP additivity check failed")
    probabilities = calibration.predict_proba(margins.reshape(-1, 1))[:, 1]
    calibrated = contributions.astype(float) * slope
    calibrated[:, -1] += intercept
    reconstructed = np.exp(-np.logaddexp(0, -calibrated.sum(axis=1)))
    if not np.allclose(reconstructed, probabilities, rtol=1e-5, atol=1e-6):
        raise ValueError("Calibrated prediction reconstruction failed")
    explanations = []
    for index, row in enumerate(rows):
        features = [
            {
                "feature": feature,
                "value": row["features"][feature],
                "imputed": row["features"][feature] is None,
                "transformed_value": float(values[index, position]),
                "raw_margin_shap": float(contributions[index, position]),
                "calibrated_log_odds_contribution": float(calibrated[index, position]),
            }
            for position, feature in enumerate(FEATURES)
        ]
        ranked = sorted(
            features, key=lambda f: abs(f["calibrated_log_odds_contribution"]), reverse=True
        )
        explanations.append(
            {
                "cik": row["cik"],
                "cutoff": row["cutoff"],
                "target": TARGET,
                "status": "UNAPPROVED_RESEARCH_EXPLANATION",
                "method": "exact_tree_shap",
                "contribution_units": "calibrated_log_odds_not_probability_points",
                "risk_probability": float(probabilities[index]),
                "prediction_category": "negative_operating_cash_flow"
                if probabilities[index] >= threshold
                else "nonnegative_operating_cash_flow",
                "threshold": threshold,
                "raw_margin": float(margins[index]),
                "raw_base_value": float(contributions[index, -1]),
                "calibrated_base_value": float(calibrated[index, -1]),
                "features": features,
                "top_contributing_features": ranked,
                "positive_contributors": [
                    f for f in ranked if f["calibrated_log_odds_contribution"] > 0
                ],
                "negative_contributors": [
                    f for f in ranked if f["calibrated_log_odds_contribution"] < 0
                ],
                "feature_sources": row.get("feature_sources", {}),
                "inference_available": False,
            }
        )
    return explanations


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
        label = row["label_source"]
        if (
            str(label["period_start"]) != row["label_start"]
            or str(label["period_end"]) != row["label_end"]
            or not Decimal(label["value"]).is_finite()
            or int(Decimal(label["value"]) < 0) != row["label"]
            or timestamp(str(label["source"]["first_observed_at"])) > available
            or timestamp(str(label["reported_available_at"])) > available
        ):
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
    if dataset.get("dataset_hash") != fingerprint(
        {k: v for k, v in dataset.items() if k != "dataset_hash"}
    ):
        reasons.append("dataset_checksum_mismatch")
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
        "seed": 17,
        "packages": {name: version(name) for name in ("xgboost", "scikit-learn", "numpy")},
        "selection_metric": "validation_average_precision",
        "calibration": "validation_sigmoid",
        "threshold_selection": "validation_f1",
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

    def sensitivity(
        fitting: list[dict[str, Any]], held_out: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if any(
            min(Counter(r["label"] for r in group).get(c, 0) for c in (0, 1)) < 20
            for group in (fitting, held_out)
        ):
            return {"status": "INSUFFICIENT_CLASS_COVERAGE"}
        sx, sy = arrays(fitting)
        hx, hy = arrays(held_out)
        pipeline = make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True),
            StandardScaler(),
            LogisticRegression(max_iter=2000, random_state=17),
        )
        pipeline.fit(sx, sy)
        return {
            "status": "EVALUATED",
            "model": "logistic_baseline",
            "training_rows": len(fitting),
            "evaluation_rows": len(held_out),
            "metrics": metrics(hy, pipeline.predict_proba(hx)[:, 1], 0.5),
        }

    # Walk-forward diagnostics use development data only, with purged label availability.
    development = splits["train"] + splits["validation"]
    walk_forward = []
    for year in sorted({timestamp(r["cutoff"]).year for r in development}):
        boundary = validation.replace(year=year, month=1, day=1, hour=0, minute=0, second=0)
        following = boundary.replace(year=year + 1)
        fitting = [
            r
            for r in development
            if timestamp(r["label_available_at"]) <= boundary - timedelta(days=30)
        ]
        held_out = [r for r in development if boundary <= timestamp(r["cutoff"]) < following]
        walk_forward.append({"year": year, **sensitivity(fitting, held_out)})
    held_issuers = {r["cik"] for r in splits["test"]}
    disjoint = sensitivity(
        [r for r in splits["train"] if r["cik"] not in held_issuers], splits["test"]
    )

    # Cluster bootstrap preserves repeated outcomes belonging to each issuer.
    rng = np.random.default_rng(17)
    issuer_indices = {
        cik: [i for i, r in enumerate(splits["test"]) if r["cik"] == cik]
        for cik in sorted(held_issuers)
    }
    issuer_names = list(issuer_indices)
    samples: list[dict[str, Any]] = []
    for _ in range(200):
        indices = [
            i
            for cik in rng.choice(issuer_names, len(issuer_names), replace=True)
            for i in issuer_indices[cik]
        ]
        if len(set(ty[indices])) == 2:
            samples.append(metrics(ty[indices], prediction[indices], threshold))
    intervals = (
        {
            key: np.quantile([s[key] for s in samples], [0.025, 0.975]).tolist()
            for key in ("pr_auc_average_precision", "roc_auc", "brier", "precision", "recall")
        }
        if samples
        else {}
    )
    calibration_bins = []
    for lower in np.arange(0, 1, 0.1):
        mask = (prediction >= lower) & (prediction < lower + 0.1)
        if lower > 0.89:
            mask = prediction >= lower
        if mask.any():
            calibration_bins.append(
                {
                    "lower": float(lower),
                    "count": int(mask.sum()),
                    "mean_probability": float(prediction[mask].mean()),
                    "observed_frequency": float(ty[mask].mean()),
                }
            )
    industries = {
        r["cik"]: str(r.get("sic", "unknown"))[:1] for r in dataset["cohort"].get("candidates", [])
    }
    subgroups = {}
    for group in sorted({industries.get(r["cik"], "unknown") for r in splits["test"]}):
        indices = [
            i for i, r in enumerate(splits["test"]) if industries.get(r["cik"], "unknown") == group
        ]
        subgroups[group] = {
            "rows": len(indices),
            "metrics": metrics(ty[indices], prediction[indices], threshold)
            if min(Counter(ty[indices]).get(c, 0) for c in (0, 1)) >= 20
            else None,
        }
    report = {
        "gate": admission,
        "threshold": threshold,
        "test": {
            "prevalence": metrics(ty, np.full(len(ty), y.mean()), 0.5),
            "logistic": metrics(ty, logistic.predict_proba(tx)[:, 1], 0.5),
            "xgboost": metrics(ty, prediction, threshold),
        },
        "release_status": "UNAPPROVED_RESEARCH_ARTIFACT",
        "walk_forward": walk_forward,
        "issuer_disjoint": disjoint,
        "issuer_bootstrap_95_percent_intervals": intervals,
        "bootstrap_valid_samples": len(samples),
        "calibration_bins": calibration_bins,
        "sic_subgroups": subgroups,
        "release_blockers": ["independent_model_and_data_review_required"],
        "test_issuers": len({r["cik"] for r in splits["test"]}),
        "test_prevalence": float(ty.mean()),
        "test_explanations": explain_predictions(model, tx, splits["test"], calibration, threshold),
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
    manifest["limitations"] = [
        "Negative operating cash flow is not bankruptcy probability",
        "Research only; independent release review required",
    ]
    manifest["checksums"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.iterdir()
        if p.name != "manifest.json"
    }
    manifest["manifest_hash"] = fingerprint(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return report
