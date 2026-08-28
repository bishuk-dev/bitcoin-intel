from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq

from bitcoin_intel.benchmarking.scenarios import ScenarioConfig, write_scenario_bundle
from bitcoin_intel.features import build_features
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
    assert any(len(record["input_addresses"]) > 1 for record in records)
    assert any(len(record["output_addresses"]) >= 6 for record in records)
    assert len(records) > config.transaction_count


def test_scenario_truth_never_enters_canonical_graph_or_features(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    write_scenario_bundle(bundle, ScenarioConfig(transaction_count=30, seed=9))
    source_records = json.loads((bundle / "source.json").read_text(encoding="utf-8"))
    forbidden = {"scenario_class", "structural_truth", "not_criminal_ground_truth"}
    assert all(forbidden.isdisjoint(record) for record in source_records)

    dataset = tmp_path / "dataset"
    ingest_file(bundle / "source.json", dataset)
    features = tmp_path / "features"
    build_features(dataset, features)
    graph_import = tmp_path / "graph-import"
    prepare_graph_import(dataset, graph_import)

    parquet_paths = list(dataset.rglob("*.parquet"))
    parquet_paths.extend(features.rglob("*.parquet"))
    parquet_paths.extend(graph_import.glob("*.parquet"))
    for path in parquet_paths:
        assert forbidden.isdisjoint(pq.read_schema(path).names)
