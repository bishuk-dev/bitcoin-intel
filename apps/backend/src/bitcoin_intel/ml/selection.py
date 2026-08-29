from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_PERFORMANCE_TOLERANCE = 0.03


@dataclass(frozen=True, slots=True)
class SelectionEvidence:
    experiment_id: str
    model: str
    macro_f1: float
    minimum_scenario_recall: float
    weak_pattern_recall: float
    multiclass_brier_score: float
    stability_macro_f1_std: float
    training_seconds: float
    inference_seconds: float
    artifact_bytes: int
    explainability_suitability: float
    offline_deployment_simplicity: float


def rank_supervised_candidates(
    candidates: list[SelectionEvidence],
) -> list[dict[str, Any]]:
    """Rank near-best models using a documented multi-criterion decision matrix."""

    if not candidates:
        raise ValueError("model selection requires at least one candidate")
    for candidate in candidates:
        _validate_evidence(candidate)
    best_macro_f1 = max(candidate.macro_f1 for candidate in candidates)
    eligible = [
        candidate
        for candidate in candidates
        if candidate.macro_f1 >= best_macro_f1 - _PERFORMANCE_TOLERANCE
    ]
    maximum_costs = {
        "training": max(candidate.training_seconds for candidate in eligible) or 1.0,
        "inference": max(candidate.inference_seconds for candidate in eligible) or 1.0,
        "artifact": max(candidate.artifact_bytes for candidate in eligible) or 1,
    }
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        eligible_for_selection = candidate in eligible
        efficiency = (
            1.0
            - candidate.training_seconds / maximum_costs["training"]
            + 1.0
            - candidate.inference_seconds / maximum_costs["inference"]
            + 1.0
            - candidate.artifact_bytes / maximum_costs["artifact"]
        ) / 3.0
        reliability = max(0.0, 1.0 - candidate.multiclass_brier_score / 2.0)
        stability = max(0.0, 1.0 - candidate.stability_macro_f1_std / 0.10)
        score = (
            0.35 * candidate.macro_f1
            + 0.15 * candidate.minimum_scenario_recall
            + 0.15 * candidate.weak_pattern_recall
            + 0.10 * reliability
            + 0.10 * stability
            + 0.05 * efficiency
            + 0.05 * candidate.explainability_suitability
            + 0.05 * candidate.offline_deployment_simplicity
        )
        ranked.append(
            {
                **asdict(candidate),
                "eligible_for_selection": eligible_for_selection,
                "selection_score": score if eligible_for_selection else None,
                "criterion": (
                    "within 0.03 macro-F1 of the best validation result, then weighted across "
                    "scenario balance, weak recall, calibration, stability, cost, diagnostics, "
                    "and offline deployment"
                ),
            }
        )
    return sorted(
        ranked,
        key=lambda row: (
            bool(row["eligible_for_selection"]),
            float(row["selection_score"] or -1.0),
            float(row["macro_f1"]),
        ),
        reverse=True,
    )


def write_model_selection(
    path: Path,
    *,
    preferred_supervised_experiment_id: str,
    preferred_anomaly_experiment_id: str,
    fallback_supervised_experiment_id: str,
    selection_metrics: dict[str, Any],
    selection_reason: str,
    alternatives_considered: list[dict[str, Any]],
    feature_schema_version: str,
    challenge_profile: str,
) -> None:
    document = {
        "selection_schema_version": "1.0.0",
        "preferred_supervised_experiment_id": preferred_supervised_experiment_id,
        "preferred_anomaly_experiment_id": preferred_anomaly_experiment_id,
        "fallback_supervised_experiment_id": fallback_supervised_experiment_id,
        "selection_metrics": selection_metrics,
        "selection_reason": selection_reason,
        "alternatives_considered": alternatives_considered,
        "feature_schema_version": feature_schema_version,
        "challenge_profile": challenge_profile,
        "semantics": {
            "model_probability_is_risk": False,
            "anomaly_score_is_risk": False,
        },
    }
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _validate_evidence(candidate: SelectionEvidence) -> None:
    bounded = (
        candidate.macro_f1,
        candidate.minimum_scenario_recall,
        candidate.weak_pattern_recall,
        candidate.explainability_suitability,
        candidate.offline_deployment_simplicity,
    )
    if any(value < 0.0 or value > 1.0 for value in bounded):
        raise ValueError("selection evidence scores must be in [0, 1]")
    if candidate.multiclass_brier_score < 0 or candidate.stability_macro_f1_std < 0:
        raise ValueError("selection reliability metrics must be non-negative")
    if min(candidate.training_seconds, candidate.inference_seconds, candidate.artifact_bytes) < 0:
        raise ValueError("selection cost metrics must be non-negative")
