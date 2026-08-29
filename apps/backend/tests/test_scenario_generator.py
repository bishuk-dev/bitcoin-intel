from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq

from bitcoin_intel.benchmarking.scenarios import ScenarioConfig, write_scenario_bundle
from bitcoin_intel.features import build_features_v1
from bitcoin_intel.graph.import_builder import prepare_graph_import
from bitcoin_intel.ingestion import ingest_file


def test_scenario_generation_is_deterministic_and_structurally_varied(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    config = ScenarioConfig(transaction_count=100, seed=17)
    first_summary = write_scenario_bundle(first, config)
    second_summary = write_scenario_bundle(second, config)

    assert (first / "source.json").read_bytes() == (second / "source.json").read_bytes()
    assert (first / "scenario-truth.json").read_bytes() == (
        second / "scenario-truth.json"
    ).read_bytes()
    assert first_summary.source_sha256 == second_summary.source_sha256
    assert set(first_summary.scenario_counts) == {
        "baseline",
        "high_fan_out_pattern",
        "rapid_sequence_pattern",
        "shared_network_pattern",
        "high_value_pattern",
    }
    records = json.loads((first / "source.json").read_text(encoding="utf-8"))
    truth = json.loads((first / "scenario-truth.json").read_text(encoding="utf-8"))
    assert any(len(record["input_addresses"]) > 1 for record in records)
    assert any(len(record["output_addresses"]) >= 6 for record in records)
    assert len(records) > config.transaction_count
    assert truth["truth_schema_version"] == "1.1.0"
    assert len({row["scenario_group_id"] for row in truth["transactions"]}) == 5


def test_related_scenario_structures_do_not_cross_group_boundaries(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    write_scenario_bundle(bundle, ScenarioConfig(transaction_count=40, seed=21, group_size=10))
    truth = json.loads((bundle / "scenario-truth.json").read_text(encoding="utf-8"))

    address_groups: dict[str, set[str]] = {}
    endpoint_groups: dict[str, set[str]] = {}
    for row in truth["transactions"]:
        group_id = row["scenario_group_id"]
        structural = row["structural_truth"]
        for address in (structural["chain_input"], structural["chain_output"]):
            address_groups.setdefault(address, set()).add(group_id)
        for endpoints in structural["observed_endpoints"]:
            for endpoint in endpoints:
                endpoint_groups.setdefault(endpoint, set()).add(group_id)

    assert all(len(groups) == 1 for groups in address_groups.values())
    assert all(len(groups) == 1 for groups in endpoint_groups.values())


def test_scenario_truth_never_enters_canonical_graph_or_features(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    write_scenario_bundle(bundle, ScenarioConfig(transaction_count=30, seed=9))
    source_records = json.loads((bundle / "source.json").read_text(encoding="utf-8"))
    forbidden = {
        "scenario_class",
        "scenario_group_id",
        "structural_truth",
        "not_criminal_ground_truth",
    }
    assert all(forbidden.isdisjoint(record) for record in source_records)

    dataset = tmp_path / "dataset"
    ingest_file(bundle / "source.json", dataset)
    features = tmp_path / "features"
    build_features_v1(dataset, features)
    graph_import = tmp_path / "graph-import"
    prepare_graph_import(dataset, graph_import)

    parquet_paths = list(dataset.rglob("*.parquet"))
    parquet_paths.extend(features.rglob("*.parquet"))
    parquet_paths.extend(graph_import.glob("*.parquet"))
    for path in parquet_paths:
        assert forbidden.isdisjoint(pq.read_schema(path).names)
