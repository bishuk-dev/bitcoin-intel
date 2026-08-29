from __future__ import annotations

import numpy as np
import pytest

from bitcoin_intel.ml.dataset import audit_feature_columns
from bitcoin_intel.ml.evaluation import supervised_metrics
from bitcoin_intel.ml.models import FEATURE_FAMILIES, MLExperimentError
from bitcoin_intel.ml.splitting import make_split


def test_feature_selection_policy_rejects_identity_and_evaluation_columns() -> None:
    audit_feature_columns(FEATURE_FAMILIES["all-eligible"])

    for forbidden in ("txid", "first_observed_at", "scenario_class", "scenario_group_id"):
        with pytest.raises(MLExperimentError, match="leakage audit rejected"):
            audit_feature_columns(("input_count", forbidden))


def test_group_split_never_places_a_group_in_multiple_partitions() -> None:
    classes = np.asarray(["a", "b", "c"] * 30, dtype=np.str_)
    groups = np.asarray([f"group-{group}" for group in range(10) for _ in range(9)], dtype=np.str_)
    split = make_split("group", len(classes), 42, labels=classes, groups=groups)

    assert split.metadata["group_audit"]["overlapping_group_count"] == 0
    group_partitions: dict[str, set[str]] = {}
    for group, partition in zip(groups.tolist(), split.membership.tolist(), strict=True):
        group_partitions.setdefault(group, set()).add(partition)
    assert all(len(partitions) == 1 for partitions in group_partitions.values())


def test_temporal_split_keeps_later_timestamp_buckets_out_of_training() -> None:
    times = np.asarray(
        [np.datetime64("2026-01-01") + np.timedelta64(index // 2, "h") for index in range(40)]
    )
    split = make_split("temporal", len(times), 42, times=times)

    train = times[split.membership == "train"]
    validation = times[split.membership == "validation"]
    test = times[split.membership == "test"]
    assert train.max() <= validation.min()
    assert validation.max() <= test.min()
    assert split.metadata["temporal_audit"]["ordered"] is True


def test_supervised_metrics_match_hand_constructed_perfect_predictions() -> None:
    classes = np.asarray(["baseline", "high_value_pattern", "rapid_sequence_pattern"])
    truth = np.asarray(["baseline", "baseline", "high_value_pattern", "rapid_sequence_pattern"])
    probabilities = np.asarray(
        [
            [0.9, 0.05, 0.05],
            [0.8, 0.1, 0.1],
            [0.05, 0.9, 0.05],
            [0.05, 0.05, 0.9],
        ],
        dtype=np.float64,
    )
    metrics = supervised_metrics(truth, truth.copy(), probabilities, classes)

    assert metrics["macro_f1"] == 1.0
    assert metrics["balanced_accuracy"] == 1.0
    assert metrics["confusion_matrix"] == [[2, 0, 0], [0, 1, 0], [0, 0, 1]]
    assert all(value["precision"] == 1.0 for value in metrics["per_class"].values())
