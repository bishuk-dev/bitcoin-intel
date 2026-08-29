from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq
import pytest

from bitcoin_intel.entity.models import MANIFEST_FILE_NAME, PART_FILE_NAME, EntityBuildConfig
from bitcoin_intel.entity.pipeline import EntityBuildError, build_entity_hypotheses
from bitcoin_intel.entity.validation import validate_entity_store
from bitcoin_intel.ingestion.cli import main


def _rows(root: Path, table: str) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], pq.read_table(root / table / PART_FILE_NAME).to_pylist())


def test_conservative_mih_suppresses_collaboration_and_exposes_transitivity(
    entity_fixture: dict[str, Path | EntityBuildConfig],
) -> None:
    root = entity_fixture["entities"]
    assert isinstance(root, Path)
    memberships = {str(row["address"]): row for row in _rows(root, "candidate_memberships")}
    assert memberships["A"]["candidate_id"] == memberships["B"]["candidate_id"]
    assert memberships["B"]["candidate_id"] == memberships["C"]["candidate_id"]
    assert memberships["C"]["transitive_only"] is True
    assert len({memberships[address]["candidate_id"] for address in "DEFG"}) == 4

    collaborative = {str(row["txid"]): row for row in _rows(root, "collaborative_transactions")}
    assert collaborative["3" * 64]["collaborative_tx_suspected"] is True
    suppressed = [
        row for row in _rows(root, "ownership_evidence") if row["evidence_source_id"] == "3" * 64
    ]
    assert suppressed and all(row["suppressed"] and not row["merge_selected"] for row in suppressed)

    supporting = [
        row for row in _rows(root, "ownership_evidence") if row["strength_class"] == "SUPPORTING"
    ]
    assert supporting and all(not row["merge_selected"] for row in supporting)
    assert any({row["address_a"], row["address_b"]} == {"A", "D"} for row in supporting)
    candidates = {str(row["candidate_id"]): row for row in _rows(root, "candidate_entities")}
    abc = candidates[str(memberships["A"]["candidate_id"])]
    assert abc["fragile_bridge_count"] == 2
    assert abc["robustness_score"] == 0.0


def test_communities_are_separate_and_hdbscan_noise_is_preserved(
    entity_fixture: dict[str, Path | EntityBuildConfig],
) -> None:
    root = entity_fixture["entities"]
    assert isinstance(root, Path)
    behavioral = _rows(root, "behavioral_communities")
    topological = _rows(root, "topological_communities")
    assert behavioral
    assert all(
        row["is_noise"] and row["behavioral_community_id"] is None and row["community_size"] == 0
        for row in behavioral
    )
    assert {row["address"] for row in behavioral} == {row["address"] for row in topological}
    assert all(row["topological_community_id"] for row in topological)


def test_entity_output_is_deterministic_valid_and_non_overwriting(
    entity_fixture: dict[str, Path | EntityBuildConfig], tmp_path: Path
) -> None:
    dataset = entity_fixture["dataset"]
    features = entity_fixture["features"]
    first = entity_fixture["entities"]
    config = entity_fixture["config"]
    assert isinstance(dataset, Path)
    assert isinstance(features, Path)
    assert isinstance(first, Path)
    assert isinstance(config, EntityBuildConfig)
    second = tmp_path / "second"
    build_entity_hypotheses(dataset, features, second, config)
    first_manifest = json.loads((first / MANIFEST_FILE_NAME).read_text(encoding="utf-8"))
    second_manifest = json.loads((second / MANIFEST_FILE_NAME).read_text(encoding="utf-8"))
    assert first_manifest["entity_dataset_id"] == second_manifest["entity_dataset_id"]
    assert {name: row["sha256"] for name, row in first_manifest["output_tables"].items()} == {
        name: row["sha256"] for name, row in second_manifest["output_tables"].items()
    }
    assert validate_entity_store(first, dataset, features).is_valid
    with pytest.raises(EntityBuildError, match="will not be overwritten"):
        build_entity_hypotheses(dataset, features, first, config)


def test_validation_detects_membership_tampering(
    entity_fixture: dict[str, Path | EntityBuildConfig],
) -> None:
    root = entity_fixture["entities"]
    dataset = entity_fixture["dataset"]
    features = entity_fixture["features"]
    assert isinstance(root, Path)
    assert isinstance(dataset, Path)
    assert isinstance(features, Path)
    manifest_path = root / MANIFEST_FILE_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["output_tables"]["candidate_memberships"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = validate_entity_store(root, dataset, features)
    assert not report.is_valid
    assert "ENTITY_FILE_HASH_MISMATCH" in {issue.code for issue in report.issues}


def test_entity_cli_builds_and_validates(
    entity_fixture: dict[str, Path | EntityBuildConfig],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = entity_fixture["dataset"]
    features = entity_fixture["features"]
    assert isinstance(dataset, Path)
    assert isinstance(features, Path)
    output = tmp_path / "cli-entities"
    assert (
        main(
            [
                "entity",
                "build",
                "--dataset",
                str(dataset),
                "--features",
                str(features),
                "--output",
                str(output),
                "--behavioral-min-cluster-size",
                "100",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "entity",
                "validate",
                "--entities",
                str(output),
                "--dataset",
                str(dataset),
                "--features",
                str(features),
            ]
        )
        == 0
    )
    output_text = capsys.readouterr().out
    assert '"entity_dataset_id"' in output_text
    assert '"valid": true' in output_text
