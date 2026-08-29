from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.model_selection import GroupShuffleSplit, train_test_split

from bitcoin_intel.ml.models import MLExperimentError

_SPLIT_NAMES = ("train", "validation", "test")


@dataclass(frozen=True, slots=True)
class SplitAssignment:
    membership: np.ndarray[Any, np.dtype[np.str_]]
    metadata: dict[str, Any]

    def indices(self, split: str) -> np.ndarray[Any, np.dtype[np.int64]]:
        if split not in _SPLIT_NAMES:
            raise ValueError(f"unknown split: {split}")
        return np.flatnonzero(self.membership == split)


def make_split(
    strategy: str,
    row_count: int,
    seed: int,
    *,
    labels: np.ndarray[Any, np.dtype[np.str_]] | None = None,
    groups: np.ndarray[Any, np.dtype[np.str_]] | None = None,
    times: np.ndarray[Any, np.dtype[np.datetime64]] | None = None,
) -> SplitAssignment:
    if row_count < 7:
        raise MLExperimentError("train/validation/test splitting requires at least seven rows")
    indices = np.arange(row_count, dtype=np.int64)
    if strategy == "random-stratified":
        membership = _random_split(indices, seed, labels)
        diagnostic_only = True
    elif strategy == "group":
        if groups is None:
            raise MLExperimentError("group-aware splitting requires scenario group metadata")
        membership = _group_split(indices, groups, labels, seed)
        diagnostic_only = False
    elif strategy == "temporal":
        if times is None:
            raise MLExperimentError("temporal splitting requires entity timestamps")
        membership = _temporal_split(indices, times)
        diagnostic_only = False
    else:
        raise MLExperimentError(f"unsupported split strategy: {strategy}")

    _validate_membership(membership)
    if labels is not None:
        _validate_class_coverage(membership, labels)
    metadata: dict[str, Any] = {
        "strategy": strategy,
        "seed": seed,
        "fractions": {"train": 0.70, "validation": 0.15, "test": 0.15},
        "counts": {name: int(np.sum(membership == name)) for name in _SPLIT_NAMES},
        "diagnostic_only": diagnostic_only,
    }
    if strategy == "group" and groups is not None:
        metadata["group_audit"] = validate_group_separation(membership, groups)
    if strategy == "temporal" and times is not None:
        metadata["temporal_audit"] = validate_temporal_order(membership, times)
    return SplitAssignment(membership=membership, metadata=metadata)


def validate_group_separation(
    membership: np.ndarray[Any, np.dtype[np.str_]],
    groups: np.ndarray[Any, np.dtype[np.str_]],
) -> dict[str, Any]:
    group_sets = {name: set(groups[membership == name].tolist()) for name in _SPLIT_NAMES}
    overlap = (
        (group_sets["train"] & group_sets["validation"])
        | (group_sets["train"] & group_sets["test"])
        | (group_sets["validation"] & group_sets["test"])
    )
    if overlap:
        raise MLExperimentError(
            f"group leakage detected across splits ({len(overlap)} overlapping groups)"
        )
    return {
        "overlapping_group_count": 0,
        "group_counts": {name: len(group_sets[name]) for name in _SPLIT_NAMES},
    }


def validate_temporal_order(
    membership: np.ndarray[Any, np.dtype[np.str_]],
    times: np.ndarray[Any, np.dtype[np.datetime64]],
) -> dict[str, Any]:
    bounds: dict[str, dict[str, str]] = {}
    for name in _SPLIT_NAMES:
        selected = times[membership == name]
        if not len(selected):
            raise MLExperimentError(f"temporal split {name} partition is empty")
        bounds[name] = {"min": str(selected.min()), "max": str(selected.max())}
    if not (
        times[membership == "train"].max() <= times[membership == "validation"].min()
        and times[membership == "validation"].max() <= times[membership == "test"].min()
    ):
        raise MLExperimentError("temporal split ordering is invalid")
    return {
        "ordered": True,
        "time_bounds": bounds,
        "limitation": (
            "Chronological row separation does not make a shared snapshot/cutoff feature store "
            "a rolling point-in-time dataset."
        ),
    }


def _random_split(
    indices: np.ndarray[Any, np.dtype[np.int64]],
    seed: int,
    labels: np.ndarray[Any, np.dtype[np.str_]] | None,
) -> np.ndarray[Any, np.dtype[np.str_]]:
    stratify = labels if labels is not None else None
    try:
        train, remainder = train_test_split(
            indices,
            test_size=0.30,
            random_state=seed,
            shuffle=True,
            stratify=stratify,
        )
        remainder_labels = labels[remainder] if labels is not None else None
        validation, test = train_test_split(
            remainder,
            test_size=0.50,
            random_state=seed + 1,
            shuffle=True,
            stratify=remainder_labels,
        )
    except ValueError as error:
        raise MLExperimentError(f"random stratified split is not feasible: {error}") from error
    return _membership(len(indices), train, validation, test)


def _group_split(
    indices: np.ndarray[Any, np.dtype[np.int64]],
    groups: np.ndarray[Any, np.dtype[np.str_]],
    labels: np.ndarray[Any, np.dtype[np.str_]] | None,
    seed: int,
) -> np.ndarray[Any, np.dtype[np.str_]]:
    if len(set(groups.tolist())) < 7:
        raise MLExperimentError("group-aware splitting requires at least seven distinct groups")
    for attempt in range(100):
        first = GroupShuffleSplit(n_splits=1, test_size=0.30, random_state=seed + attempt)
        train_positions, remainder_positions = next(first.split(indices, labels, groups))
        remainder_groups = groups[remainder_positions]
        second = GroupShuffleSplit(
            n_splits=1,
            test_size=0.50,
            random_state=seed + 10_000 + attempt,
        )
        validation_relative, test_relative = next(
            second.split(remainder_positions, None, remainder_groups)
        )
        membership = _membership(
            len(indices),
            indices[train_positions],
            indices[remainder_positions[validation_relative]],
            indices[remainder_positions[test_relative]],
        )
        if labels is None or _has_all_classes(membership, labels):
            validate_group_separation(membership, groups)
            return membership
    raise MLExperimentError(
        "could not construct a group-aware split containing every class in every partition"
    )


def _temporal_split(
    indices: np.ndarray[Any, np.dtype[np.int64]],
    times: np.ndarray[Any, np.dtype[np.datetime64]],
) -> np.ndarray[Any, np.dtype[np.str_]]:
    if np.isnat(times).any():
        raise MLExperimentError("temporal split contains undefined timestamps")
    order = np.argsort(times, kind="stable")
    train_end = _advance_equal_times(order, times, max(1, int(len(order) * 0.70)))
    validation_end = _advance_equal_times(order, times, max(train_end + 1, int(len(order) * 0.85)))
    if train_end >= validation_end or validation_end >= len(order):
        raise MLExperimentError("timestamp distribution cannot produce three non-empty partitions")
    membership = _membership(
        len(indices), order[:train_end], order[train_end:validation_end], order[validation_end:]
    )
    validate_temporal_order(membership, times)
    return membership


def _advance_equal_times(
    order: np.ndarray[Any, np.dtype[np.int64]],
    times: np.ndarray[Any, np.dtype[np.datetime64]],
    boundary: int,
) -> int:
    while boundary < len(order) and times[order[boundary - 1]] == times[order[boundary]]:
        boundary += 1
    return boundary


def _membership(
    row_count: int,
    train: np.ndarray[Any, np.dtype[np.int64]],
    validation: np.ndarray[Any, np.dtype[np.int64]],
    test: np.ndarray[Any, np.dtype[np.int64]],
) -> np.ndarray[Any, np.dtype[np.str_]]:
    result = np.full(row_count, "", dtype="<U10")
    result[train] = "train"
    result[validation] = "validation"
    result[test] = "test"
    return result


def _validate_membership(membership: np.ndarray[Any, np.dtype[np.str_]]) -> None:
    invalid = set(membership.tolist()) - set(_SPLIT_NAMES)
    if invalid:
        raise MLExperimentError(f"split assignment is incomplete: {sorted(invalid)}")
    if any(not np.any(membership == name) for name in _SPLIT_NAMES):
        raise MLExperimentError("split assignment contains an empty partition")


def _validate_class_coverage(
    membership: np.ndarray[Any, np.dtype[np.str_]],
    labels: np.ndarray[Any, np.dtype[np.str_]],
) -> None:
    expected = set(labels.tolist())
    for name in _SPLIT_NAMES:
        actual = set(labels[membership == name].tolist())
        missing = sorted(expected - actual)
        if missing:
            raise MLExperimentError(
                f"{name} split is missing configured classes: {', '.join(missing)}"
            )


def _has_all_classes(
    membership: np.ndarray[Any, np.dtype[np.str_]],
    labels: np.ndarray[Any, np.dtype[np.str_]],
) -> bool:
    expected = set(labels.tolist())
    return all(set(labels[membership == name].tolist()) == expected for name in _SPLIT_NAMES)
