from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitcoin_intel.entity.models import EntityBuildConfig
from bitcoin_intel.entity.pipeline import build_entity_hypotheses
from bitcoin_intel.features.pipeline import build_features_v1
from bitcoin_intel.ingestion import ingest_file
from tests.factories import make_source_record


@pytest.fixture
def entity_fixture(tmp_path: Path) -> dict[str, Path | EntityBuildConfig]:
    source = tmp_path / "entity-source.json"
    records = [
        make_source_record(
            txid="1" * 64,
            input_addresses=["A", "B"],
            input_amounts=["1", "1"],
            output_addresses=["OA"],
            output_amounts=["1.99"],
            fee="0.01",
            src_ip="203.0.113.1",
        ),
        make_source_record(
            txid="2" * 64,
            input_addresses=["B", "C"],
            input_amounts=["1", "1"],
            output_addresses=["OB"],
            output_amounts=["1.99"],
            fee="0.01",
            src_ip="203.0.113.2",
        ),
        make_source_record(
            txid="3" * 64,
            input_addresses=["D", "E", "F", "G"],
            input_amounts=["0.5", "0.5", "0.5", "0.5"],
            output_addresses=["OD", "OE", "OF", "OG"],
            output_amounts=["0.4975", "0.4975", "0.4975", "0.4975"],
            fee="0.01",
            src_ip="203.0.113.1",
        ),
        make_source_record(
            txid="4" * 64,
            input_addresses=["P"],
            input_amounts=["1"],
            output_addresses=["OP"],
            output_amounts=["0.99"],
            fee="0.01",
            src_ip="203.0.113.1",
        ),
    ]
    source.write_text(json.dumps(records), encoding="utf-8")
    dataset = tmp_path / "canonical"
    features = tmp_path / "features"
    entities = tmp_path / "entities"
    ingest_file(source, dataset)
    build_features_v1(dataset, features)
    config = EntityBuildConfig(
        behavioral_min_cluster_size=100,
        behavioral_min_samples=3,
    )
    build_entity_hypotheses(dataset, features, entities, config)
    return {
        "dataset": dataset,
        "features": features,
        "entities": entities,
        "config": config,
    }
