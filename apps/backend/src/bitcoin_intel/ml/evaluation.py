from __future__ import annotations

import math
from typing import Any

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    balanced_accuracy_score,
    confusion_matrix,
    log_loss,
    precision_recall_fscore_support,
    roc_auc_score,
)
from sklearn.preprocessing import label_binarize

from bitcoin_intel.benchmarking.scenarios import SCENARIO_NAMES
from bitcoin_intel.ml.models import MLExperimentError


def supervised_metrics(
    truth: np.ndarray[Any, np.dtype[np.str_]],
    predictions: np.ndarray[Any, np.dtype[np.str_]],
    probabilities: np.ndarray[Any, np.dtype[np.float64]],
    classes: np.ndarray[Any, np.dtype[np.str_]],
) -> dict[str, Any]:
    if probabilities.shape != (len(truth), len(classes)):
        raise MLExperimentError("supervised probability matrix has an unexpected shape")
    precision, recall, f1, support = precision_recall_fscore_support(
        truth,
        predictions,
        labels=classes,
        zero_division=0,
    )
    _, _, macro_f1, _ = precision_recall_fscore_support(
        truth, predictions, average="macro", zero_division=0
    )
    _, _, weighted_f1, _ = precision_recall_fscore_support(
        truth, predictions, average="weighted", zero_division=0
    )
    binary_truth = label_binarize(truth, classes=classes)
    if binary_truth.shape[1] != len(classes):
        raise MLExperimentError("multiclass evaluation requires every configured class")
    per_class: dict[str, Any] = {}
    for index, class_name in enumerate(classes.tolist()):
        class_truth = binary_truth[:, index]
        per_class[class_name] = {
            "precision": float(precision[index]),
            "recall": float(recall[index]),
            "f1": float(f1[index]),
            "support": int(support[index]),
            "roc_auc_ovr": float(roc_auc_score(class_truth, probabilities[:, index])),
            "average_precision": float(
                average_precision_score(class_truth, probabilities[:, index])
            ),
        }
    brier = float(np.mean(np.sum((probabilities - binary_truth) ** 2, axis=1)))
    return {
        "balanced_accuracy": float(balanced_accuracy_score(truth, predictions)),
        "macro_f1": float(macro_f1),
        "weighted_f1": float(weighted_f1),
        "roc_auc_ovr_macro": float(
            roc_auc_score(truth, probabilities, labels=classes, multi_class="ovr", average="macro")
        ),
        "average_precision_macro": float(
            np.mean([metrics["average_precision"] for metrics in per_class.values()])
        ),
        "multiclass_brier_score": brier,
        "log_loss": float(log_loss(truth, probabilities, labels=classes)),
        "classes": classes.tolist(),
        "per_class": per_class,
        "confusion_matrix": confusion_matrix(truth, predictions, labels=classes).tolist(),
    }


def anomaly_metrics(
    truth: np.ndarray[Any, np.dtype[np.str_]],
    anomaly_scores: np.ndarray[Any, np.dtype[np.float64]],
) -> dict[str, Any]:
    if len(truth) != len(anomaly_scores):
        raise MLExperimentError("anomaly evaluation arrays differ in length")
    binary = (truth != "baseline").astype(np.int8)
    if len(np.unique(binary)) != 2:
        raise MLExperimentError("anomaly evaluation requires baseline and injected scenarios")
    ranking = np.argsort(-anomaly_scores, kind="stable")
    scenario_distributions = {
        scenario: _score_summary(anomaly_scores[truth == scenario])
        for scenario in SCENARIO_NAMES
        if np.any(truth == scenario)
    }
    per_scenario: dict[str, Any] = {}
    baseline_mask = truth == "baseline"
    for scenario in SCENARIO_NAMES:
        if scenario == "baseline" or not np.any(truth == scenario):
            continue
        mask = baseline_mask | (truth == scenario)
        scenario_binary = (truth[mask] == scenario).astype(np.int8)
        scenario_metrics: dict[str, Any] = {
            "roc_auc_vs_baseline": float(roc_auc_score(scenario_binary, anomaly_scores[mask])),
            "average_precision_vs_baseline": float(
                average_precision_score(scenario_binary, anomaly_scores[mask])
            ),
            "support": int(np.sum(truth == scenario)),
        }
        for percentage in (1, 5, 10):
            count = max(1, math.ceil(len(truth) * percentage / 100))
            scenario_metrics[f"top_{percentage}_percent_capture"] = float(
                np.sum(truth[ranking[:count]] == scenario) / np.sum(truth == scenario)
            )
        per_scenario[scenario] = scenario_metrics
    top_budget: dict[str, Any] = {}
    for percentage in (1, 5, 10):
        count = max(1, math.ceil(len(truth) * percentage / 100))
        selected = ranking[:count]
        top_budget[f"top_{percentage}_percent"] = {
            "row_count": count,
            "injected_capture": float(binary[selected].sum() / binary.sum()),
            "precision": float(binary[selected].mean()),
        }
    return {
        "score_direction": "higher_is_more_anomalous",
        "comparison": "baseline_vs_injected_scenario",
        "roc_auc": float(roc_auc_score(binary, anomaly_scores)),
        "average_precision": float(average_precision_score(binary, anomaly_scores)),
        "top_budget": top_budget,
        "score_distributions": scenario_distributions,
        "per_scenario": per_scenario,
    }


def recall_by_intensity(
    truth: np.ndarray[Any, np.dtype[np.str_]],
    predictions: np.ndarray[Any, np.dtype[np.str_]],
    intensities: np.ndarray[Any, np.dtype[np.str_]],
) -> dict[str, dict[str, float | int]]:
    if not (len(truth) == len(predictions) == len(intensities)):
        raise MLExperimentError("intensity evaluation arrays differ in length")
    result: dict[str, dict[str, float | int]] = {}
    for scenario in SCENARIO_NAMES:
        if scenario == "baseline":
            continue
        for intensity in ("weak", "medium", "strong"):
            mask = (truth == scenario) & (intensities == intensity)
            if np.any(mask):
                result.setdefault(scenario, {})[intensity] = float(
                    np.mean(predictions[mask] == truth[mask])
                )
                result[scenario][f"{intensity}_support"] = int(np.sum(mask))
    return result


def unsupervised_score_summary(
    anomaly_scores: np.ndarray[Any, np.dtype[np.float64]],
) -> dict[str, Any]:
    return {
        "score_direction": "higher_is_more_anomalous",
        "evaluation_truth_available": False,
        "score_distribution": _score_summary(anomaly_scores),
    }


def _score_summary(scores: np.ndarray[Any, np.dtype[np.float64]]) -> dict[str, float | int]:
    if not len(scores):
        raise MLExperimentError("cannot summarize an empty anomaly-score set")
    return {
        "count": len(scores),
        "min": float(np.min(scores)),
        "mean": float(np.mean(scores)),
        "median": float(np.median(scores)),
        "p95": float(np.percentile(scores, 95)),
        "max": float(np.max(scores)),
    }
