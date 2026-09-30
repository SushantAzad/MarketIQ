import copy
import json
import math
from datetime import UTC, datetime

from app.risk.cohort import classify
from app.risk.dataset import FEATURES, TARGET, VERSION
from app.risk.training import explain_predictions, gate, temporal_split, train, valid_row
from app.services.normalization import fingerprint


def dataset():
    rows = []
    for year in (2012, 2015, 2018):
        for index in range(120):
            cutoff = f"{year}-06-01T00:00:00+00:00"
            available = f"{year + 2}-03-01T00:00:00+00:00"
            positive = index % 3 == 0
            label_source = {
                "value": "-10" if positive else "10",
                "period_start": f"{year + 1}-01-01",
                "period_end": f"{year + 1}-12-31",
                "reported_available_at": available,
                "source": {"fact_id": index + 1, "first_observed_at": available},
            }
            rows.append(
                {
                    "cik": str(year * 1000 + index),
                    "cutoff": cutoff,
                    "features": {f: (index % 7) / 7 for f in FEATURES},
                    "feature_sources": {
                        "revenue": {
                            "value": "100",
                            "reported_available_at": cutoff,
                            "source": {"fact_id": index + 1, "first_observed_at": cutoff},
                        }
                    },
                    "label": int(positive),
                    "label_start": label_source["period_start"],
                    "label_end": label_source["period_end"],
                    "label_available_at": available,
                    "label_source": label_source,
                }
            )
    result = {
        "version": VERSION,
        "target": TARGET,
        "feature_order": list(FEATURES),
        "as_of": "2022-01-01T00:00:00+00:00",
        "rows": rows,
        "cohort": {"historical_inactive_coverage_verified": True},
    }
    result["dataset_hash"] = fingerprint(result)
    return result


VALIDATION = datetime(2015, 1, 1, tzinfo=UTC)
TEST = datetime(2018, 1, 1, tzinfo=UTC)


def test_gate_blocks_sparse_data_without_artifacts(tmp_path):
    data = dataset()
    data["rows"] = []
    output = tmp_path / "model"
    report = train(data, output, VALIDATION, TEST)
    assert report["status"] == "BLOCKED_BY_DATA"
    assert "dataset_checksum_mismatch" in report["reasons"]
    assert not output.exists()


def test_temporal_lineage_and_purged_labels():
    data = dataset()
    assert gate(data, VALIDATION, TEST)["status"] == "READY_FOR_OFFLINE_EXPERIMENT"
    row = copy.deepcopy(data["rows"][0])
    row["feature_sources"]["revenue"]["source"]["first_observed_at"] = data["as_of"]
    assert not valid_row(row, datetime(2022, 1, 1, tzinfo=UTC))
    row = copy.deepcopy(data["rows"][0])
    row["label_source"]["value"] = "10"
    assert not valid_row(row, datetime(2022, 1, 1, tzinfo=UTC))
    row["label_available_at"] = "2014-12-20T00:00:00+00:00"
    assert temporal_split([row], VALIDATION, TEST)["train"] == []


def test_cohort_excludes_financial_and_foreign_issuers():
    payload = {
        "sic": "3571",
        "stateOfIncorporation": "DE",
        "filings": {"recent": {"form": ["10-K"]}},
    }
    assert classify(payload) is None
    assert classify(payload | {"sic": "6021"}) == "financial_sector"
    assert classify(payload | {"stateOfIncorporation": "E9"}) is not None


def test_offline_experiment_remains_unapproved(tmp_path):
    # Synthetic fixtures exercise serialization and evaluation, never production data.
    import pytest

    pytest.importorskip("xgboost")
    output = tmp_path / "experiment"
    report = train(dataset(), output, VALIDATION, TEST)
    assert report["release_status"] == "UNAPPROVED_RESEARCH_ARTIFACT"
    assert report["bootstrap_valid_samples"] == 200
    assert report["issuer_disjoint"]["status"] == "EVALUATED"
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["approved"] is False
    assert manifest["inference_available"] is False
    assert "model.ubj" in manifest["checksums"]
    explanations = report["test_explanations"]
    assert len(explanations) == 120
    for explanation in explanations:
        features = explanation["features"]
        margin = explanation["raw_base_value"] + sum(f["raw_margin_shap"] for f in features)
        assert margin == pytest.approx(explanation["raw_margin"], abs=1e-5)
        log_odds = explanation["calibrated_base_value"] + sum(
            f["calibrated_log_odds_contribution"] for f in features
        )
        assert 1 / (1 + math.exp(-log_odds)) == pytest.approx(
            explanation["risk_probability"], abs=1e-6
        )
        assert explanation["inference_available"] is False
    assert json.loads((output / "evaluation.json").read_text())["test_explanations"] == explanations


def test_shap_calibration_direction_missing_inputs_and_validation():
    import numpy as np
    import pytest

    xgb = pytest.importorskip("xgboost")
    from sklearn.linear_model import LogisticRegression

    x = np.array([[i / 20] * len(FEATURES) for i in range(40)])
    y = np.array([int(i >= 20) for i in range(40)])
    model = xgb.XGBClassifier(n_estimators=5, max_depth=2, n_jobs=1).fit(x, y)
    margins = model.predict(x, output_margin=True).reshape(-1, 1)
    calibration = LogisticRegression().fit(margins, 1 - y)
    assert calibration.coef_[0, 0] < 0
    rows = dataset()["rows"][:40]
    rows[0]["features"][FEATURES[0]] = None
    results = explain_predictions(model, x, rows, calibration, 0.5)
    assert results[0]["features"][0]["imputed"] is True
    assert results[0]["prediction_category"] == "negative_operating_cash_flow"
    assert results[-1]["prediction_category"] == "nonnegative_operating_cash_flow"
    for result in results:
        for feature in result["features"]:
            assert feature["calibrated_log_odds_contribution"] == pytest.approx(
                feature["raw_margin_shap"] * calibration.coef_[0, 0]
            )
        assert all(
            f["calibrated_log_odds_contribution"] > 0 for f in result["positive_contributors"]
        )
        assert all(
            f["calibrated_log_odds_contribution"] < 0 for f in result["negative_contributors"]
        )
    with pytest.raises(ValueError, match="matrix"):
        explain_predictions(model, x[:, :-1], rows, calibration, 0.5)
    with pytest.raises(ValueError, match="matrix"):
        explain_predictions(model, np.full_like(x, np.nan), rows, calibration, 0.5)
    with pytest.raises(ValueError, match="threshold"):
        explain_predictions(model, x, rows, calibration, 1.0)
    calibration.coef_[0, 0] = np.nan
    with pytest.raises(ValueError, match="calibration parameters"):
        explain_predictions(model, x, rows, calibration, 0.5)
