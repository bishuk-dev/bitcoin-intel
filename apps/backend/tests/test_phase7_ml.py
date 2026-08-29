from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pytest

from bitcoin_intel.benchmarking.scenarios import ScenarioConfig, write_scenario_bundle
from bitcoin_intel.features import build_features_v1
from bitcoin_intel.ingestion import ingest_file
from bitcoin_intel.ml.estimators import PCAReconstructionDetector
from bitcoin_intel.ml.models import ENRICHMENT_FEATURES, FEATURE_FAMILIES, ExperimentConfig
from bitcoin_intel.ml.selection import SelectionEvidence, rank_supervised_candidates
from bitcoin_intel.ml.training import _build_pipeline
from bitcoin_intel.ml.tuning import run_validation_search


@pytest.mark.parametrize(
    ("model", "overrides"),
    [
        ("hist-gradient-boosting", (("max_iter", 8),)),
        ("xgboost", (("n_estimators", 8),)),
        ("lightgbm", (("n_estimators", 8),)),
    ],
)
def test_advanced_classifier_probability_and_reload_contract(
    tmp_path: Path, model: str, overrides: tuple[tuple[str, int], ...]
) -> None:
    generator = np.random.default_rng(42)
    values = generator.normal(size=(150, 8))
    labels = np.asarray(
        [
            "baseline",
            "high_fan_out_pattern",
            "rapid_sequence_pattern",
            "shared_network_pattern",
            "high_value_pattern",
        ]
        * 30
    )
    config = ExperimentConfig(
        feature_path=Path("features"),
        output_root=Path("experiments"),
        truth_path=Path("truth.json"),
        experiment_type="scenario",
        model=model,
        parameter_overrides=overrides,
    )
    pipeline, parameters = _build_pipeline(config, train_row_count=len(values))
    pipeline.fit(values, labels)
    probabilities = pipeline.predict_proba(values[:7])
    artifact = tmp_path / f"{model}.joblib"
    joblib.dump(pipeline, artifact)
    reloaded = joblib.load(artifact)

    assert probabilities.shape == (7, 5)
    assert probabilities.sum(axis=1) == pytest.approx(np.ones(7))
    assert np.array_equal(pipeline.predict(values[:7]), reloaded.predict(values[:7]))
    assert parameters[overrides[0][0]] == overrides[0][1]
    if model in {"xgboost", "lightgbm"}:
        assert parameters["n_jobs"] == 4


def test_pca_reconstruction_error_is_higher_off_training_subspace(tmp_path: Path) -> None:
    axis = np.linspace(-2.0, 2.0, 100)
    training = np.column_stack((axis, axis * 2.0, axis * -0.5))
    detector = PCAReconstructionDetector(explained_variance=0.90).fit(training)
    normal, anomalous = training[50], np.asarray([0.0, 0.0, 20.0])
    project_scores = -detector.score_samples(np.vstack((normal, anomalous)))
    artifact = tmp_path / "pca.joblib"
    joblib.dump(detector, artifact)

    assert project_scores[1] > project_scores[0]
    assert np.array_equal(
        joblib.load(artifact).score_samples(training[:3]), detector.score_samples(training[:3])
    )


def test_feature_v2_families_include_enrichment_only_when_requested() -> None:
    assert set(ENRICHMENT_FEATURES).isdisjoint(FEATURE_FAMILIES["transaction-only"])
    assert set(ENRICHMENT_FEATURES).isdisjoint(FEATURE_FAMILIES["transaction-network"])
    assert set(ENRICHMENT_FEATURES) <= set(FEATURE_FAMILIES["transaction-network-enrichment"])
    assert set(ENRICHMENT_FEATURES) <= set(FEATURE_FAMILIES["all-eligible"])


def test_selection_keeps_predictive_gate_then_uses_multiple_criteria() -> None:
    strong_expensive = _evidence("strong", 0.82, 12.0, 5_000_000)
    close_simple = _evidence("simple", 0.81, 0.2, 20_000)
    weak_tiny = _evidence("weak", 0.60, 0.01, 100)
    ranking = rank_supervised_candidates([strong_expensive, close_simple, weak_tiny])

    assert ranking[0]["model"] == "simple"
    assert ranking[-1]["model"] == "weak"
    assert ranking[-1]["eligible_for_selection"] is False


def test_validation_search_never_scores_test_rows(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    write_scenario_bundle(bundle, ScenarioConfig(transaction_count=220, seed=61, group_size=10))
    dataset = tmp_path / "dataset"
    ingest_file(bundle / "source.json", dataset)
    features = tmp_path / "features"
    build_features_v1(dataset, features)
    results = run_validation_search(
        [
            ExperimentConfig(
                feature_path=features,
                output_root=tmp_path / "unused",
                truth_path=bundle / "scenario-truth.json",
                experiment_type="scenario",
                model="logistic-regression",
                seed=42,
            ),
            ExperimentConfig(
                feature_path=features,
                output_root=tmp_path / "unused",
                truth_path=bundle / "scenario-truth.json",
                experiment_type="scenario",
                model="logistic-regression",
                seed=42,
                parameter_overrides=(("C", 0.3),),
            ),
        ]
    )

    assert len(results) == 2
    assert all(result.validation_rows > 0 and result.test_rows_seen == 0 for result in results)


def _evidence(model: str, macro_f1: float, training: float, size: int) -> SelectionEvidence:
    return SelectionEvidence(
        experiment_id=f"experiment-{model}",
        model=model,
        macro_f1=macro_f1,
        minimum_scenario_recall=macro_f1,
        weak_pattern_recall=macro_f1,
        multiclass_brier_score=0.3,
        stability_macro_f1_std=0.01,
        training_seconds=training,
        inference_seconds=training / 10,
        artifact_bytes=size,
        explainability_suitability=0.9 if model == "simple" else 0.5,
        offline_deployment_simplicity=1.0 if model == "simple" else 0.4,
    )
