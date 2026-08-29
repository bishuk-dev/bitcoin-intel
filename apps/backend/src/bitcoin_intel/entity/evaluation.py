from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from sklearn.metrics import adjusted_mutual_info_score, adjusted_rand_score

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.entity.models import MANIFEST_FILE_NAME, PART_FILE_NAME, EntityBuildConfig
from bitcoin_intel.entity.pipeline import analyze_ownership

ENTITY_TRUTH_SCHEMA_VERSION = "1.0.0"


class EntityEvaluationError(RuntimeError):
    """Raised when entity evaluation inputs are incomplete or unsafe."""


def evaluate_entity_store(
    dataset_path: Path,
    entity_path: Path,
    truth_path: Path,
    partition: str = "test",
    config: EntityBuildConfig | None = None,
) -> dict[str, Any]:
    if partition not in {"development", "validation", "test"}:
        raise EntityEvaluationError("partition must be development, validation, or test")
    dataset = AnalyticalDataset(dataset_path)
    truth = _load_truth(truth_path)
    truth_mapping, partition_addresses = _truth_partition(truth, partition)
    if len(partition_addresses) < 2:
        raise EntityEvaluationError(f"truth partition {partition!r} has fewer than two addresses")
    effective_config = config or _load_store_config(entity_path)
    analysis, _, _ = analyze_ownership(dataset, effective_config)
    final_mapping = _load_final_mapping(entity_path)
    missing = sorted(set(truth_mapping) - set(analysis.addresses))
    if missing:
        raise EntityEvaluationError(
            f"truth contains an address absent from canonical data: {missing[0]}"
        )
    final_missing = sorted(set(truth_mapping) - set(final_mapping))
    if final_missing:
        raise EntityEvaluationError(
            f"entity store is missing a truth-covered address: {final_missing[0]}"
        )
    collaborative_rows = truth.get("collaborative_transactions")
    if not isinstance(collaborative_rows, list):
        raise EntityEvaluationError("entity truth collaborative_transactions must be a list")
    mappings = {
        "raw_mih": analysis.raw_mapping,
        "collaborative_suppression": analysis.suppressed_mapping,
        "final_conservative": final_mapping,
    }
    return {
        "evaluation_schema_version": "1.0.0",
        "truth_schema_version": ENTITY_TRUTH_SCHEMA_VERSION,
        "partition": partition,
        "address_count": len(partition_addresses),
        "entity_count": len({truth_mapping[address] for address in partition_addresses}),
        "precision_first": True,
        "build_configuration": effective_config.semantic_dict(),
        "baselines": {
            name: _clustering_metrics(
                truth_mapping,
                predicted,
                partition_addresses,
                collaborative_rows,
                partition,
            )
            for name, predicted in mappings.items()
        },
    }


def _load_store_config(entity_path: Path) -> EntityBuildConfig:
    try:
        manifest = json.loads(
            (entity_path.expanduser().resolve(strict=True) / MANIFEST_FILE_NAME).read_text(
                encoding="utf-8"
            )
        )
        raw = manifest["build_configuration"]
        return EntityBuildConfig(
            collaborative_min_inputs=int(raw["collaborative_min_inputs"]),
            collaborative_min_outputs=int(raw["collaborative_min_outputs"]),
            collaborative_min_equal_outputs=int(raw["collaborative_min_equal_outputs"]),
            collaborative_min_equal_fraction=float(raw["collaborative_min_equal_fraction"]),
            behavioral_min_cluster_size=int(raw["behavioral_min_cluster_size"]),
            behavioral_min_samples=int(raw["behavioral_min_samples"]),
            leiden_seed=int(raw["leiden_seed"]),
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise EntityEvaluationError(f"entity build configuration is unreadable: {error}") from error


def _clustering_metrics(
    truth: dict[str, str],
    predicted: dict[str, str],
    addresses: tuple[str, ...],
    collaborative_rows: list[Any],
    partition: str,
) -> dict[str, Any]:
    intersections: Counter[tuple[str, str]] = Counter(
        (truth[address], predicted[address]) for address in addresses
    )
    true_sizes = Counter(truth[address] for address in addresses)
    predicted_sizes = Counter(predicted[address] for address in addresses)
    true_positive_pairs = sum(_choose_two(count) for count in intersections.values())
    predicted_pairs = sum(_choose_two(count) for count in predicted_sizes.values())
    true_pairs = sum(_choose_two(count) for count in true_sizes.values())
    pairwise_precision = true_positive_pairs / predicted_pairs if predicted_pairs else 1.0
    pairwise_recall = true_positive_pairs / true_pairs if true_pairs else 1.0
    pairwise_f1 = _f1(pairwise_precision, pairwise_recall)
    b3_precision = sum(
        intersections[(truth[address], predicted[address])] / predicted_sizes[predicted[address]]
        for address in addresses
    ) / len(addresses)
    b3_recall = sum(
        intersections[(truth[address], predicted[address])] / true_sizes[truth[address]]
        for address in addresses
    ) / len(addresses)
    b3_f1 = _f1(b3_precision, b3_recall)
    true_labels = [truth[address] for address in addresses]
    predicted_labels = [predicted[address] for address in addresses]
    predicted_truth_sets: defaultdict[str, set[str]] = defaultdict(set)
    true_predicted_sets: defaultdict[str, set[str]] = defaultdict(set)
    for address in addresses:
        predicted_truth_sets[predicted[address]].add(truth[address])
        true_predicted_sets[truth[address]].add(predicted[address])
    per_entity: list[dict[str, Any]] = []
    for entity_id in sorted(true_sizes):
        best_cluster, overlap = max(
            (
                (cluster_id, intersections[(entity_id, cluster_id)])
                for cluster_id in true_predicted_sets[entity_id]
            ),
            key=lambda value: (value[1], value[0]),
        )
        per_entity.append(
            {
                "truth_entity_id": entity_id,
                "address_count": true_sizes[entity_id],
                "best_candidate_id": best_cluster,
                "best_overlap": overlap,
                "precision": overlap / predicted_sizes[best_cluster],
                "recall": overlap / true_sizes[entity_id],
                "fragment_count": len(true_predicted_sets[entity_id]),
            }
        )
    collaborative_total = 0
    collaborative_false_merges = 0
    address_set = set(addresses)
    for raw in collaborative_rows:
        if not isinstance(raw, dict) or raw.get("partition") != partition:
            continue
        raw_addresses = raw.get("input_addresses")
        if not isinstance(raw_addresses, list):
            raise EntityEvaluationError("collaborative truth row has malformed input_addresses")
        tx_addresses = [str(value) for value in raw_addresses if str(value) in address_set]
        if len(tx_addresses) < 2:
            continue
        collaborative_total += 1
        cluster_truth: defaultdict[str, set[str]] = defaultdict(set)
        for address in tx_addresses:
            cluster_truth[predicted[address]].add(truth[address])
        collaborative_false_merges += any(len(values) > 1 for values in cluster_truth.values())
    return {
        "pairwise_precision": pairwise_precision,
        "pairwise_recall": pairwise_recall,
        "pairwise_f1": pairwise_f1,
        "b_cubed_precision": b3_precision,
        "b_cubed_recall": b3_recall,
        "b_cubed_f1": b3_f1,
        "adjusted_rand_index": float(adjusted_rand_score(true_labels, predicted_labels)),
        "adjusted_mutual_information": float(
            adjusted_mutual_info_score(true_labels, predicted_labels)
        ),
        "overmerged_candidate_rate": sum(
            len(values) > 1 for values in predicted_truth_sets.values()
        )
        / len(predicted_truth_sets),
        "fragmented_entity_rate": sum(len(values) > 1 for values in true_predicted_sets.values())
        / len(true_predicted_sets),
        "collaborative_false_merge_rate": (
            collaborative_false_merges / collaborative_total if collaborative_total else 0.0
        ),
        "collaborative_transaction_count": collaborative_total,
        "candidate_count": len(predicted_sizes),
        "per_entity": per_entity,
    }


def _load_truth(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EntityEvaluationError(f"entity truth is unreadable or malformed: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("truth_schema_version") != ENTITY_TRUTH_SCHEMA_VERSION
    ):
        raise EntityEvaluationError("entity truth schema version is unsupported")
    if document.get("evaluation_only") is not True:
        raise EntityEvaluationError("entity truth must be explicitly evaluation-only")
    return document


def _truth_partition(
    document: dict[str, Any], partition: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    rows = document.get("entities")
    if not isinstance(rows, list):
        raise EntityEvaluationError("entity truth entities must be a list")
    mapping: dict[str, str] = {}
    selected: list[str] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise EntityEvaluationError("entity truth contains a malformed entity")
        entity_id = raw.get("entity_id")
        addresses = raw.get("addresses")
        entity_partition = raw.get("partition")
        if (
            not isinstance(entity_id, str)
            or not isinstance(addresses, list)
            or not addresses
            or entity_partition not in {"development", "validation", "test"}
        ):
            raise EntityEvaluationError("entity truth row has malformed identity or partition")
        for value in addresses:
            address = str(value)
            if address in mapping:
                raise EntityEvaluationError(
                    f"truth address belongs to multiple entities: {address}"
                )
            mapping[address] = entity_id
            if entity_partition == partition:
                selected.append(address)
    return mapping, tuple(sorted(selected))


def _load_final_mapping(entity_path: Path) -> dict[str, str]:
    try:
        root = entity_path.expanduser().resolve(strict=True)
        manifest = json.loads((root / MANIFEST_FILE_NAME).read_text(encoding="utf-8"))
        raw = manifest["output_tables"]["candidate_memberships"]
        path = root / "candidate_memberships" / PART_FILE_NAME
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise EntityEvaluationError(f"entity store is unreadable or malformed: {error}") from error
    if not isinstance(raw, dict) or raw.get("sha256") != _sha256_file(path):
        raise EntityEvaluationError("candidate memberships differ from the entity manifest")
    table = pq.read_table(path, columns=["address", "candidate_id"])
    return {
        str(address): str(candidate_id)
        for address, candidate_id in zip(
            table["address"].to_pylist(), table["candidate_id"].to_pylist(), strict=True
        )
    }


def _choose_two(value: int) -> int:
    return math.comb(value, 2) if value >= 2 else 0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
