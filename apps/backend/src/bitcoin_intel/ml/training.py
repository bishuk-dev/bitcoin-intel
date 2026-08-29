from __future__ import annotations

import ctypes
import os
import platform
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import sklearn
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import LocalOutlierFactor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from bitcoin_intel.ml.artifacts import (
    artifact_metadata,
    create_staging_directory,
    discard_staging_directory,
    inspect_experiment,
    publish_staging_directory,
    semantic_experiment_id,
    write_json,
)
from bitcoin_intel.ml.dataset import LoadedExperimentDataset, load_experiment_dataset
from bitcoin_intel.ml.evaluation import (
    anomaly_metrics,
    supervised_metrics,
    unsupervised_score_summary,
)
from bitcoin_intel.ml.models import (
    EXPERIMENT_SCHEMA_VERSION,
    ExperimentConfig,
    ExperimentSummary,
    MLExperimentError,
)
from bitcoin_intel.ml.splitting import SplitAssignment, make_split

_PARQUET_COMPRESSION = "zstd"


class FiniteMatrixValidator(TransformerMixin, BaseEstimator):  # type: ignore[misc]
    """Fail at the preprocessing boundary if an estimator would receive NaN or infinity."""

    def fit(self, values: Any, labels: Any = None) -> FiniteMatrixValidator:
        del labels
        self._validate(values)
        return self

    def transform(self, values: Any) -> np.ndarray[Any, np.dtype[np.float64]]:
        return self._validate(values)

    @staticmethod
    def _validate(values: Any) -> np.ndarray[Any, np.dtype[np.float64]]:
        matrix = np.asarray(values, dtype=np.float64)
        if matrix.ndim != 2 or not matrix.shape[0] or not matrix.shape[1]:
            raise MLExperimentError(
                "preprocessed model matrix must be non-empty and two-dimensional"
            )
        if not np.isfinite(matrix).all():
            raise MLExperimentError("preprocessed model matrix contains NaN or infinity")
        return matrix


def run_experiment(config: ExperimentConfig) -> ExperimentSummary:
    tracemalloc.start()
    try:
        dataset = load_experiment_dataset(
            config.feature_path, config.feature_family, config.truth_path
        )
        _, loading_peak_bytes = tracemalloc.get_traced_memory()
        if config.experiment_type == "scenario":
            _validate_multiclass_truth(dataset)
        split = make_split(
            config.split_strategy,
            len(dataset.entity_ids),
            config.seed,
            labels=dataset.labels,
            groups=dataset.groups,
            times=dataset.times,
        )
        train_indices = split.indices("train")
        constant_indices = _constant_feature_indices(dataset.values[train_indices])
        active_indices = np.asarray(
            [index for index in range(dataset.values.shape[1]) if index not in constant_indices],
            dtype=np.int64,
        )
        if not len(active_indices):
            raise MLExperimentError(
                "all selected model features are constant in the training split"
            )
        active_columns = tuple(dataset.feature_columns[index] for index in active_indices)
        constant_columns = tuple(dataset.feature_columns[index] for index in constant_indices)
        values = dataset.values[:, active_indices]
        model_pipeline, hyperparameters = _build_pipeline(
            config, train_row_count=len(train_indices)
        )
        semantic_configuration = _semantic_configuration(
            config,
            dataset,
            split,
            active_columns,
            constant_columns,
            hyperparameters,
        )
        experiment_id = semantic_experiment_id(semantic_configuration)
        staging, destination = create_staging_directory(config.output_root, experiment_id)
        try:
            training_started = time.perf_counter()
            fit_labels = (
                dataset.labels[train_indices]
                if config.experiment_type == "scenario" and dataset.labels is not None
                else None
            )
            if fit_labels is None:
                model_pipeline.fit(values[train_indices])
            else:
                model_pipeline.fit(values[train_indices], fit_labels)
            training_seconds = time.perf_counter() - training_started
            inference_started = time.perf_counter()
            predictions, probabilities, anomaly_scores = _predict(
                config.experiment_type, model_pipeline, values
            )
            inference_seconds = time.perf_counter() - inference_started
            metrics = _evaluate(
                config.experiment_type,
                dataset,
                split,
                predictions,
                probabilities,
                anomaly_scores,
                model_pipeline,
            )
            runtime = {
                "feature_loading_peak_traced_bytes": loading_peak_bytes,
                "process_peak_rss_bytes": _process_peak_rss_bytes(),
                "training_seconds": training_seconds,
                "inference_seconds": inference_seconds,
            }
            _write_experiment_artifacts(
                staging,
                experiment_id,
                semantic_configuration,
                config,
                dataset,
                split,
                active_columns,
                constant_columns,
                model_pipeline,
                predictions,
                probabilities,
                anomaly_scores,
                metrics,
                runtime,
            )
            inspect_experiment(staging)
            publish_staging_directory(staging, destination)
        except Exception:
            discard_staging_directory(staging)
            raise
    finally:
        tracemalloc.stop()

    primary_name = "roc_auc" if config.experiment_type == "anomaly" else "macro_f1"
    raw_primary_value = metrics["test"].get(primary_name)
    primary_value = float(raw_primary_value) if raw_primary_value is not None else None
    return ExperimentSummary(
        output_path=destination,
        experiment_id=experiment_id,
        experiment_type=config.experiment_type,
        model=config.model,
        rows=len(dataset.entity_ids),
        feature_count=len(active_columns),
        split_counts=split.metadata["counts"],
        primary_metric_name=primary_name,
        primary_metric_value=primary_value,
    )


def _build_pipeline(
    config: ExperimentConfig, *, train_row_count: int
) -> tuple[Pipeline, dict[str, Any]]:
    if config.model == "isolation-forest":
        parameters: dict[str, Any] = {
            "n_estimators": 200,
            "max_samples": "auto",
            "contamination": "auto",
            "random_state": config.seed,
            "n_jobs": 1,
        }
        estimator = IsolationForest(**parameters)
        steps: list[tuple[str, Any]] = [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("finite", FiniteMatrixValidator()),
            ("model", estimator),
        ]
    elif config.model == "local-outlier-factor":
        parameters = {
            "n_neighbors": min(20, train_row_count - 1),
            "contamination": "auto",
            "novelty": True,
            "algorithm": "auto",
            "n_jobs": 1,
        }
        estimator = LocalOutlierFactor(**parameters)
        steps = [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("finite", FiniteMatrixValidator()),
            ("model", estimator),
        ]
    elif config.model == "logistic-regression":
        parameters = {
            "C": 1.0,
            "class_weight": "balanced",
            "max_iter": 1_000,
            "random_state": config.seed,
            "solver": "lbfgs",
            "tol": 1e-4,
        }
        estimator = LogisticRegression(**parameters)
        steps = [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("finite", FiniteMatrixValidator()),
            ("model", estimator),
        ]
    elif config.model == "random-forest":
        parameters = {
            "n_estimators": 300,
            "max_depth": 16,
            "min_samples_leaf": 2,
            "class_weight": "balanced_subsample",
            "random_state": config.seed,
            "n_jobs": 1,
        }
        estimator = RandomForestClassifier(**parameters)
        steps = [
            ("imputer", SimpleImputer(strategy="median")),
            ("finite", FiniteMatrixValidator()),
            ("model", estimator),
        ]
    else:
        raise AssertionError(f"validated model has no pipeline: {config.model}")
    return Pipeline(steps), parameters


def _predict(
    experiment_type: str,
    model_pipeline: Pipeline,
    values: np.ndarray[Any, np.dtype[np.float64]],
) -> tuple[
    np.ndarray[Any, np.dtype[np.str_]] | None,
    np.ndarray[Any, np.dtype[np.float64]] | None,
    np.ndarray[Any, np.dtype[np.float64]] | None,
]:
    if experiment_type == "scenario":
        predictions = np.asarray(model_pipeline.predict(values), dtype=np.str_)
        probabilities = np.asarray(model_pipeline.predict_proba(values), dtype=np.float64)
        if np.isnan(probabilities).any() or np.isinf(probabilities).any():
            raise MLExperimentError("supervised model produced non-finite probabilities")
        return predictions, probabilities, None
    native_scores = np.asarray(model_pipeline.score_samples(values), dtype=np.float64)
    anomaly_scores = -native_scores
    if np.isnan(anomaly_scores).any() or np.isinf(anomaly_scores).any():
        raise MLExperimentError("anomaly model produced non-finite scores")
    return None, None, anomaly_scores


def _evaluate(
    experiment_type: str,
    dataset: LoadedExperimentDataset,
    split: SplitAssignment,
    predictions: np.ndarray[Any, np.dtype[np.str_]] | None,
    probabilities: np.ndarray[Any, np.dtype[np.float64]] | None,
    anomaly_scores: np.ndarray[Any, np.dtype[np.float64]] | None,
    model_pipeline: Pipeline,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for split_name in ("validation", "test"):
        indices = split.indices(split_name)
        if experiment_type == "scenario":
            if dataset.labels is None or predictions is None or probabilities is None:
                raise AssertionError("supervised evaluation inputs are missing")
            classes = np.asarray(model_pipeline.named_steps["model"].classes_, dtype=np.str_)
            result[split_name] = supervised_metrics(
                dataset.labels[indices], predictions[indices], probabilities[indices], classes
            )
        elif anomaly_scores is not None and dataset.labels is not None:
            result[split_name] = anomaly_metrics(dataset.labels[indices], anomaly_scores[indices])
        elif anomaly_scores is not None:
            result[split_name] = unsupervised_score_summary(anomaly_scores[indices])
        else:
            raise AssertionError("anomaly evaluation scores are missing")
    return result


def _semantic_configuration(
    config: ExperimentConfig,
    dataset: LoadedExperimentDataset,
    split: SplitAssignment,
    active_columns: tuple[str, ...],
    constant_columns: tuple[str, ...],
    hyperparameters: dict[str, Any],
) -> dict[str, Any]:
    feature_build = dataset.feature_manifest["build_configuration"]
    return {
        "experiment_type": config.experiment_type,
        "entity_type": config.entity_type,
        "feature_dataset_id": dataset.feature_manifest["feature_dataset_id"],
        "feature_schema_version": dataset.feature_manifest["feature_schema_version"],
        "feature_family": config.feature_family,
        "feature_columns": list(active_columns),
        "constant_columns_excluded": list(constant_columns),
        "feature_mode": feature_build["temporal_mode"],
        "feature_cutoff": feature_build.get("cutoff"),
        "truth_sha256": (
            dataset.truth_metadata["path_sha256"] if dataset.truth_metadata is not None else None
        ),
        "split": {
            "strategy": config.split_strategy,
            "seed": config.seed,
            "membership_sha256": _split_digest(dataset.entity_ids, split.membership),
            "counts": split.metadata["counts"],
        },
        "seed": config.seed,
        "model": {
            "name": config.model,
            "class": type(
                _build_pipeline(config, train_row_count=split.metadata["counts"]["train"])[
                    0
                ].named_steps["model"]
            ).__name__,
            "hyperparameters": hyperparameters,
        },
        "library_versions": _library_versions(),
    }


def _write_experiment_artifacts(
    staging: Path,
    experiment_id: str,
    semantic_configuration: dict[str, Any],
    config: ExperimentConfig,
    dataset: LoadedExperimentDataset,
    split: SplitAssignment,
    active_columns: tuple[str, ...],
    constant_columns: tuple[str, ...],
    model_pipeline: Pipeline,
    predictions: np.ndarray[Any, np.dtype[np.str_]] | None,
    probabilities: np.ndarray[Any, np.dtype[np.float64]] | None,
    anomaly_scores: np.ndarray[Any, np.dtype[np.float64]] | None,
    metrics: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    evaluation = staging / "evaluation"
    evaluation.mkdir()
    write_json(staging / "metrics.json", metrics)
    write_json(staging / "split.json", split.metadata)
    write_json(
        staging / "feature-columns.json",
        {
            "requested_family": config.feature_family,
            "active_model_columns": list(active_columns),
            "constant_training_columns_excluded": list(constant_columns),
            "identity_columns_excluded": ["txid"],
            "timestamp_columns_excluded": ["first_observed_at", "last_observed_at"],
            "evaluation_only_columns_excluded": ["scenario_class", "scenario_group_id"],
        },
    )
    pq.write_table(
        pa.table({"txid": dataset.entity_ids, "split": split.membership}),
        staging / "splits.parquet",
        compression=_PARQUET_COMPRESSION,
    )
    _write_predictions(
        staging / "predictions.parquet",
        config.experiment_type,
        dataset,
        split,
        predictions,
        probabilities,
        anomaly_scores,
        model_pipeline,
    )
    if dataset.labels is not None:
        truth_columns: dict[str, Any] = {
            "txid": dataset.entity_ids,
            "scenario_class": dataset.labels,
        }
        if dataset.groups is not None:
            truth_columns["scenario_group_id"] = dataset.groups
        pq.write_table(
            pa.table(truth_columns),
            evaluation / "scenario-truth.parquet",
            compression=_PARQUET_COMPRESSION,
        )
    diagnostics = _model_diagnostics(model_pipeline, active_columns)
    write_json(evaluation / "model-diagnostics.json", diagnostics)
    write_json(
        evaluation / "confusion-matrix.json",
        {
            name: {
                "classes": value.get("classes"),
                "matrix": value.get("confusion_matrix"),
            }
            for name, value in metrics.items()
            if "confusion_matrix" in value
        },
    )
    write_json(
        evaluation / "per-scenario-metrics.json",
        {
            name: value.get("per_scenario", value.get("per_class", {}))
            for name, value in metrics.items()
        },
    )
    joblib.dump(model_pipeline, staging / "model.joblib", compress=3)
    relative_paths = [
        Path("metrics.json"),
        Path("split.json"),
        Path("feature-columns.json"),
        Path("splits.parquet"),
        Path("predictions.parquet"),
        Path("model.joblib"),
        Path("evaluation/model-diagnostics.json"),
        Path("evaluation/confusion-matrix.json"),
        Path("evaluation/per-scenario-metrics.json"),
    ]
    if dataset.labels is not None:
        relative_paths.append(Path("evaluation/scenario-truth.parquet"))
    artifacts = artifact_metadata(staging, relative_paths)
    runtime["model_artifact_bytes"] = artifacts["model.joblib"]["bytes"]
    manifest = {
        "experiment_schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "experiment_type": config.experiment_type,
        "entity_type": config.entity_type,
        "feature_dataset_id": dataset.feature_manifest["feature_dataset_id"],
        "feature_schema_version": dataset.feature_manifest["feature_schema_version"],
        "feature_columns": list(active_columns),
        "feature_family": config.feature_family,
        "feature_mode": semantic_configuration["feature_mode"],
        "feature_cutoff": semantic_configuration["feature_cutoff"],
        "split_strategy": config.split_strategy,
        "split_counts": split.metadata["counts"],
        "seed": config.seed,
        "model": semantic_configuration["model"],
        "library_versions": semantic_configuration["library_versions"],
        "metrics": metrics,
        "runtime": runtime,
        "truth_metadata": dataset.truth_metadata,
        "semantic_configuration": semantic_configuration,
        "artifacts": artifacts,
        "model_loading_security": (
            "model.joblib is a trusted locally-generated artifact only; never deserialize "
            "downloaded, uploaded, or otherwise untrusted joblib/pickle data"
        ),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    write_json(staging / "experiment.json", manifest)


def _write_predictions(
    path: Path,
    experiment_type: str,
    dataset: LoadedExperimentDataset,
    split: SplitAssignment,
    predictions: np.ndarray[Any, np.dtype[np.str_]] | None,
    probabilities: np.ndarray[Any, np.dtype[np.float64]] | None,
    anomaly_scores: np.ndarray[Any, np.dtype[np.float64]] | None,
    model_pipeline: Pipeline,
) -> None:
    columns: dict[str, Any] = {"txid": dataset.entity_ids, "split": split.membership}
    if experiment_type == "scenario":
        if predictions is None or probabilities is None:
            raise AssertionError("supervised predictions are missing")
        columns["predicted_scenario"] = predictions
        classes = np.asarray(model_pipeline.named_steps["model"].classes_, dtype=np.str_)
        for index, class_name in enumerate(classes.tolist()):
            columns[f"scenario_probability_{class_name}"] = probabilities[:, index]
    else:
        if anomaly_scores is None:
            raise AssertionError("anomaly predictions are missing")
        columns["anomaly_score"] = anomaly_scores
    pq.write_table(pa.table(columns), path, compression=_PARQUET_COMPRESSION)


def _model_diagnostics(model_pipeline: Pipeline, columns: tuple[str, ...]) -> dict[str, Any]:
    model = model_pipeline.named_steps["model"]
    if hasattr(model, "coef_"):
        return {
            "kind": "logistic_regression_coefficients",
            "classes": np.asarray(model.classes_, dtype=np.str_).tolist(),
            "feature_columns": list(columns),
            "coefficients": np.asarray(model.coef_, dtype=np.float64).tolist(),
            "note": "coefficients apply after training-fitted median imputation and scaling",
        }
    if hasattr(model, "feature_importances_"):
        return {
            "kind": "random_forest_impurity_importance",
            "feature_importance": dict(
                zip(
                    columns,
                    np.asarray(model.feature_importances_, dtype=np.float64).tolist(),
                    strict=True,
                )
            ),
            "limitation": (
                "impurity importance is descriptive and may favor high-cardinality signals"
            ),
        }
    return {"kind": "not_available_for_baseline", "feature_columns": list(columns)}


def _constant_feature_indices(
    training_values: np.ndarray[Any, np.dtype[np.float64]],
) -> tuple[int, ...]:
    constant: list[int] = []
    for index in range(training_values.shape[1]):
        finite = training_values[:, index][~np.isnan(training_values[:, index])]
        if not len(finite) or np.min(finite) == np.max(finite):
            constant.append(index)
    return tuple(constant)


def _validate_multiclass_truth(dataset: LoadedExperimentDataset) -> None:
    if dataset.labels is None:
        raise MLExperimentError("scenario experiment has no evaluation truth")
    from bitcoin_intel.benchmarking.scenarios import SCENARIO_NAMES

    missing = sorted(set(SCENARIO_NAMES) - set(dataset.labels.tolist()))
    if missing:
        raise MLExperimentError(
            f"scenario dataset is missing configured classes: {', '.join(missing)}"
        )


def _split_digest(
    entity_ids: np.ndarray[Any, np.dtype[np.str_]],
    membership: np.ndarray[Any, np.dtype[np.str_]],
) -> str:
    import hashlib

    digest = hashlib.sha256()
    for entity_id, split in zip(entity_ids.tolist(), membership.tolist(), strict=True):
        digest.update(f"{entity_id}\0{split}\n".encode())
    return digest.hexdigest()


def _library_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pyarrow": pa.__version__,
        "scikit_learn": sklearn.__version__,
        "joblib": joblib.__version__,
    }


def _process_peak_rss_bytes() -> int | None:
    if os.name != "nt":
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)  # type: ignore[attr-defined]
            value = int(usage.ru_maxrss)
            return value if platform.system() == "Darwin" else value * 1024
        except (ImportError, OSError):
            return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
            ("private_usage", ctypes.c_size_t),
        ]

    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        windll = ctypes.windll
        get_current_process = windll.kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        get_process_memory_info = windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        get_process_memory_info.restype = ctypes.c_int
        process = get_current_process()
        if not get_process_memory_info(process, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.peak_working_set_size)
    except (AttributeError, OSError):
        return None
