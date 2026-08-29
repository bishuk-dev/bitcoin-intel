from __future__ import annotations

import json
from pathlib import Path

from bitcoin_intel.benchmarking.entity_challenge import (
    EntityChallengeConfig,
    audit_entity_challenge_bundle,
    write_entity_challenge_bundle,
)
from bitcoin_intel.entity.evaluation import evaluate_entity_store
from bitcoin_intel.entity.models import EntityBuildConfig
from bitcoin_intel.ingestion.cli import main


def test_entity_challenge_is_deterministic_leakage_free_and_entity_safe(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    config = EntityChallengeConfig(transaction_count=100, seed=91)
    first_summary = write_entity_challenge_bundle(first, config)
    second_summary = write_entity_challenge_bundle(second, config)
    assert first_summary.source_sha256 == second_summary.source_sha256
    assert first_summary.truth_sha256 == second_summary.truth_sha256
    audit = audit_entity_challenge_bundle(first)
    assert audit["status"] == "passed"
    truth = json.loads((first / "entity-truth.json").read_text(encoding="utf-8"))
    address_partitions: dict[str, set[str]] = {}
    for entity in truth["entities"]:
        for address in entity["addresses"]:
            address_partitions.setdefault(address, set()).add(entity["partition"])
    assert all(len(partitions) == 1 for partitions in address_partitions.values())
    assert set(first_summary.partition_entity_counts) == {"development", "validation", "test"}


def test_evaluation_reports_precision_gain_from_collaboration_suppression(
    entity_fixture: dict[str, Path | EntityBuildConfig],
    tmp_path: Path,
) -> None:
    dataset = entity_fixture["dataset"]
    entities = entity_fixture["entities"]
    assert isinstance(dataset, Path)
    assert isinstance(entities, Path)
    truth_path = tmp_path / "truth.json"
    truth = {
        "truth_schema_version": "1.0.0",
        "evaluation_only": True,
        "entities": [
            {"entity_id": "truth-abc", "addresses": ["A", "B", "C"], "partition": "test"},
            *[
                {"entity_id": f"truth-{address}", "addresses": [address], "partition": "test"}
                for address in "DEFG"
            ],
        ],
        "collaborative_transactions": [
            {
                "txid": "3" * 64,
                "partition": "test",
                "input_addresses": list("DEFG"),
                "participant_entity_ids": [f"truth-{address}" for address in "DEFG"],
            }
        ],
    }
    truth_path.write_text(json.dumps(truth), encoding="utf-8")
    result = evaluate_entity_store(dataset, entities, truth_path)
    raw = result["baselines"]["raw_mih"]
    suppressed = result["baselines"]["collaborative_suppression"]
    final = result["baselines"]["final_conservative"]
    assert raw["pairwise_precision"] < suppressed["pairwise_precision"]
    assert raw["collaborative_false_merge_rate"] == 1.0
    assert suppressed["collaborative_false_merge_rate"] == 0.0
    assert final["pairwise_precision"] == suppressed["pairwise_precision"]
    features = entity_fixture["features"]
    assert isinstance(features, Path)
    assert (
        main(
            [
                "entity",
                "evaluate",
                "--entities",
                str(entities),
                "--dataset",
                str(dataset),
                "--features",
                str(features),
                "--truth",
                str(truth_path),
            ]
        )
        == 0
    )
