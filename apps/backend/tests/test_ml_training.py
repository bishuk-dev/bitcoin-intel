from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import pytest
from sklearn.pipeline import Pipeline

from bitcoin_intel.benchmarking.scenarios import (
    SCENARIO_NAMES,
    ScenarioConfig,
    write_scenario_bundle,
)
from bitcoin_intel.features import build_features_v1
from bitcoin_intel.ingestion import ingest_file
from bitcoin_intel.ingestion.cli import main
from bitcoin_intel.ml import ExperimentConfig, run_experiment
from bitcoin_intel.ml.artifacts import inspect_experiment, load_trusted_local_model
from bitcoin_intel.ml.dataset import load_experiment_dataset
from bitcoin_intel.ml.models import MLExperimentError
from bitcoin_intel.ml.training import _build_pipeline


@pytest.fixture(scope="module")
def ml_dataset(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("ml-dataset")
    bundle = root / "bundle"
    write_scenario_bundle(bundle, ScenarioConfig(transaction_count=300, seed=53, group_size=10))
    dataset = root / "dataset"
    ingest_file(bundle / "source.json", dataset)
    features = root / "features"
    build_features_v1(dataset, features)
    return features, bundle / "scenario-truth.json"


def test_preprocessing_is_fit_on_training_rows_only() -> None:
    config = ExperimentConfig(
        feature_path=Path("features"),
        output_root=Path("experiments"),
        truth_path=Path("truth.json"),
        experiment_type="scenario",
        model="logistic-regression",
    )
    pipeline, _ = _build_pipeline(config, train_row_count=10)
    training = np.asarray([[0.0, np.nan], [2.0, 2.0], [4.0, 4.0], [6.0, 6.0]])
    labels = np.asarray(["a", "b", "a", "b"])
    held_out = np.asarray([[10_000.0, 10_000.0]])
    pipeline.fit(training, labels)

    imputer = pipeline.named_steps["imputer"]
    scaler = pipeline.named_steps["scaler"]
    assert imputer.statistics_.tolist() == [3.0, 4.0]
    assert scaler.mean_.tolist() == pytest.approx([3.0, 4.0])
    assert scaler.transform(imputer.transform(held_out))[0, 0] > 1_000


def test_isolation_forest_project_score_is_higher_for_far_outlier() -> None:
    config = ExperimentConfig(
        feature_path=Path("features"),
        output_root=Path("experiments"),
        experiment_type="anomaly",
        model="isolation-forest",
        split_strategy="random-stratified",
    )
    pipeline, _ = _build_pipeline(config, train_row_count=100)
    generator = np.random.default_rng(42)
    training = generator.normal(0, 0.1, size=(100, 2))
    pipeline.fit(training)
    test = np.asarray([[0.0, 0.0], [20.0, 20.0]])
    project_scores = -pipeline.score_samples(test)

    assert project_scores[1] > project_scores[0]


def test_scenario_experiment_is_reproducible_and_reloadable(
    tmp_path: Path, ml_dataset: tuple[Path, Path]
) -> None:
    features, truth = ml_dataset
    common: dict[str, Any] = {
        "feature_path": features,
        "truth_path": truth,
        "experiment_type": "scenario",
        "model": "logistic-regression",
        "split_strategy": "group",
        "seed": 42,
    }
    first = run_experiment(ExperimentConfig(output_root=tmp_path / "first", **common))
    second = run_experiment(ExperimentConfig(output_root=tmp_path / "second", **common))

    assert first.experiment_id == second.experiment_id
    assert pq.read_table(first.output_path / "splits.parquet").equals(
        pq.read_table(second.output_path / "splits.parquet")
    )
    assert pq.read_table(first.output_path / "predictions.parquet").equals(
        pq.read_table(second.output_path / "predictions.parquet")
    )
    assert (first.output_path / "metrics.json").read_bytes() == (
        second.output_path / "metrics.json"
    ).read_bytes()

    manifest = json.loads((first.output_path / "experiment.json").read_text(encoding="utf-8"))
    selected = json.loads((first.output_path / "feature-columns.json").read_text(encoding="utf-8"))
    assert manifest["feature_mode"] == "snapshot"
    assert manifest["model_loading_security"].startswith("model.joblib is a trusted")
    assert {"txid", "scenario_class", "scenario_group_id"}.isdisjoint(
        selected["active_model_columns"]
    )
    assert set(SCENARIO_NAMES) == set(manifest["metrics"]["test"]["classes"])

    model = load_trusted_local_model(first.output_path, trusted=True)
    assert isinstance(model, Pipeline)
    loaded = load_experiment_dataset(features, "all-eligible", truth)
    active = selected["active_model_columns"]
    positions = [loaded.feature_columns.index(name) for name in active]
    reloaded_predictions = np.asarray(model.predict(loaded.values[:, positions]), dtype=np.str_)
    stored_predictions = np.asarray(
        pq.read_table(first.output_path / "predictions.parquet")["predicted_scenario"].to_pylist(),
        dtype=np.str_,
    )
    assert np.array_equal(reloaded_predictions, stored_predictions)
    assert inspect_experiment(first.output_path)["valid"] is True


def test_truth_is_separate_from_model_predictions_and_cli_evaluation_is_safe(
    tmp_path: Path, ml_dataset: tuple[Path, Path]
) -> None:
    features, truth = ml_dataset
    summary = run_experiment(
        ExperimentConfig(
            feature_path=features,
            truth_path=truth,
            output_root=tmp_path / "experiments",
            experiment_type="anomaly",
            model="local-outlier-factor",
            split_strategy="group",
        )
    )
    prediction_columns = set(pq.read_schema(summary.output_path / "predictions.parquet").names)
    truth_columns = set(
        pq.read_schema(summary.output_path / "evaluation" / "scenario-truth.parquet").names
    )
    assert prediction_columns == {"txid", "split", "anomaly_score"}
    assert truth_columns == {"txid", "scenario_class", "scenario_group_id"}
    assert main(["ml", "evaluate", "--experiment", str(summary.output_path)]) == 0


def test_invalid_or_untrusted_artifact_fails_before_deserialization(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "experiment.json").write_text("{}", encoding="utf-8")
    (malformed / "model.joblib").write_bytes(b"not a pickle")

    with pytest.raises(MLExperimentError, match="schema version"):
        inspect_experiment(malformed)
    with pytest.raises(MLExperimentError, match="not explicitly trusted"):
        load_trusted_local_model(malformed)
