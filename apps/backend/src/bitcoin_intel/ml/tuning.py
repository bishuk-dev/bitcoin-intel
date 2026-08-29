from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from bitcoin_intel.ml.dataset import load_experiment_dataset
from bitcoin_intel.ml.evaluation import supervised_metrics
from bitcoin_intel.ml.models import ExperimentConfig, MLExperimentError
from bitcoin_intel.ml.splitting import make_split
from bitcoin_intel.ml.training import (
    _build_pipeline,
    _constant_feature_indices,
    _normalize_probabilities,
)


@dataclass(frozen=True, slots=True)
class ValidationSearchResult:
    model: str
    parameter_overrides: tuple[tuple[str, Any], ...]
    macro_f1: float
    balanced_accuracy: float
    training_seconds: float
    validation_rows: int
    test_rows_seen: int


def run_validation_search(configs: list[ExperimentConfig]) -> list[ValidationSearchResult]:
    """Fit deterministic candidates on train and score validation without touching test rows."""

    if not configs:
        raise ValueError("validation search requires candidate configurations")
    reference = configs[0]
    if reference.experiment_type != "scenario" or reference.truth_path is None:
        raise ValueError("validation search requires supervised scenario truth")
    if reference.split_strategy != "group":
        raise ValueError("Phase 7 selection search must use group-aware splitting")
    dataset = load_experiment_dataset(
        reference.feature_path, reference.feature_family, reference.truth_path
    )
    if dataset.labels is None:
        raise MLExperimentError("validation search has no labels")
    split = make_split(
        "group",
        len(dataset.entity_ids),
        reference.seed,
        labels=dataset.labels,
        groups=dataset.groups,
        times=dataset.times,
    )
    train = split.indices("train")
    validation = split.indices("validation")
    constant = set(_constant_feature_indices(dataset.values[train]))
    active = np.asarray(
        [index for index in range(dataset.values.shape[1]) if index not in constant],
        dtype=np.int64,
    )
    values = dataset.values[:, active]
    results: list[ValidationSearchResult] = []
    for config in configs:
        _validate_comparable(reference, config)
        pipeline, _ = _build_pipeline(config, train_row_count=len(train))
        started = time.perf_counter()
        pipeline.fit(values[train], dataset.labels[train])
        training_seconds = time.perf_counter() - started
        predictions = np.asarray(pipeline.predict(values[validation]), dtype=np.str_)
        probabilities = _normalize_probabilities(
            np.asarray(pipeline.predict_proba(values[validation]), dtype=np.float64)
        )
        classes = np.asarray(pipeline.named_steps["model"].classes_, dtype=np.str_)
        metrics = supervised_metrics(
            dataset.labels[validation], predictions, probabilities, classes
        )
        results.append(
            ValidationSearchResult(
                model=config.model,
                parameter_overrides=config.parameter_overrides,
                macro_f1=float(metrics["macro_f1"]),
                balanced_accuracy=float(metrics["balanced_accuracy"]),
                training_seconds=training_seconds,
                validation_rows=len(validation),
                test_rows_seen=0,
            )
        )
    return results


def _validate_comparable(reference: ExperimentConfig, candidate: ExperimentConfig) -> None:
    comparable = (
        candidate.feature_path == reference.feature_path
        and candidate.truth_path == reference.truth_path
        and candidate.feature_family == reference.feature_family
        and candidate.split_strategy == reference.split_strategy
        and candidate.seed == reference.seed
        and candidate.calibration == "none"
    )
    if not comparable:
        raise ValueError("validation-search candidates must share data, split, seed, and scope")
