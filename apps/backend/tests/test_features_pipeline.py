from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.features.definitions import build_definition_registry
from bitcoin_intel.features.graph import build_component_size_table
from bitcoin_intel.features.models import FEATURE_TABLES, FeatureBuildConfig
from bitcoin_intel.features.pipeline import (
    FeatureBuildError,
    _register_scoped_views,
    build_features,
)
from bitcoin_intel.features.validation import validate_feature_store
from tests.feature_fixtures import create_feature_dataset, read_feature_rows


def test_hand_calculated_features_prevent_multiplicative_joins(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    output = tmp_path / "features"
    summary = build_features(dataset, output)

    transactions = {row["txid"]: row for row in read_feature_rows(output, "transaction_features")}
    tx1 = transactions["1" * 64]
    assert summary.temporal_mode == "snapshot"
    assert tx1["input_count"] == 3
    assert tx1["output_count"] == 4
    assert tx1["network_observation_count"] == 5
    assert tx1["total_input_sats"] == 600_000_000
    assert tx1["total_output_sats"] == 590_000_000
    assert tx1["fee_sats"] == 10_000_000
    assert tx1["fee_to_input_ratio"] == pytest.approx(1 / 60)
    assert tx1["max_observations_1m"] == 3
    assert tx1["min_inter_observation_seconds"] == 0

    addresses = {row["address"]: row for row in read_feature_rows(output, "address_features")}
    address_a = addresses["AddressA"]
    assert address_a["input_occurrence_count"] == 2
    assert address_a["output_occurrence_count"] == 1
    assert address_a["unique_tx_count"] == 2
    assert address_a["network_observation_count"] == 6
    assert address_a["co_transaction_address_count"] == 6
    assert address_a["bipartite_component_size"] == 9
    assert addresses["DisconnectedF"]["bipartite_component_size"] == 3

    correlations = {
        row["address"]: row for row in read_feature_rows(output, "correlation_features")
    }
    assert correlations["AddressA"]["reused_ip_count"] == 1
    assert correlations["AddressA"]["max_transactions_per_associated_ip"] == 2


def test_ip_roles_ipv4_ipv6_ports_and_null_intervals(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    output = tmp_path / "features"
    build_features(dataset, output)
    ips = {row["ip"]: row for row in read_feature_rows(output, "ip_features")}

    shared = ips["192.0.2.10"]
    assert shared["source_observation_count"] == 4
    assert shared["destination_observation_count"] == 1
    assert shared["total_observation_count"] == 4
    assert shared["unique_tx_count"] == 2
    assert shared["unique_port_count"] == 2
    assert shared["min_inter_observation_seconds"] == 0
    assert ips["2001:db8:1::1"]["mean_inter_observation_seconds"] is None


def test_cutoff_excludes_future_observations_and_transactions(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    output = tmp_path / "cutoff-features"
    cutoff = datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
    summary = build_features(dataset, output, FeatureBuildConfig(cutoff=cutoff))

    transactions = {row["txid"]: row for row in read_feature_rows(output, "transaction_features")}
    assert summary.temporal_mode == "cutoff"
    assert set(transactions) == {"1" * 64, "3" * 64}
    assert transactions["1" * 64]["network_observation_count"] == 2
    assert transactions["1" * 64]["last_observed_at"] <= cutoff
    addresses = {row["address"]: row for row in read_feature_rows(output, "address_features")}
    assert addresses["AddressA"]["unique_tx_count"] == 1
    assert addresses["AddressA"]["bipartite_component_size"] == 7
    assert validate_feature_store(output, dataset).is_valid


def test_empty_input_output_and_singleton_statistics_are_well_defined(
    tmp_path: Path,
) -> None:
    from bitcoin_intel.ingestion import ingest_file
    from tests.factories import make_source_record

    source = tmp_path / "edge.json"
    source.write_text(
        json.dumps(
            [
                make_source_record(
                    txid="4" * 64,
                    input_addresses=[],
                    input_amounts=[],
                    output_addresses=["OnlyOutput"],
                    output_amounts=["1"],
                    fee="0",
                ),
                make_source_record(
                    txid="5" * 64,
                    input_addresses=["OnlyInput"],
                    input_amounts=["1"],
                    output_addresses=[],
                    output_amounts=[],
                    fee="0",
                    timestamp="2026-01-01T00:01:00Z",
                ),
            ]
        ),
        encoding="utf-8",
    )
    dataset = tmp_path / "dataset"
    ingest_file(source, dataset)
    output = tmp_path / "features"
    build_features(dataset, output)
    rows = {row["txid"]: row for row in read_feature_rows(output, "transaction_features")}
    assert rows["4" * 64]["input_count"] == 0
    assert rows["4" * 64]["mean_input_sats"] is None
    assert rows["5" * 64]["output_count"] == 0
    assert rows["5" * 64]["fee_to_input_ratio"] == 0.0
    assert rows["5" * 64]["input_value_std"] is None


def test_rebuild_has_identical_semantic_identity_and_parquet_hashes(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    build_features(dataset, first)
    build_features(dataset, second)
    first_manifest = json.loads((first / "feature-manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second / "feature-manifest.json").read_text(encoding="utf-8"))

    assert first_manifest["feature_dataset_id"] == second_manifest["feature_dataset_id"]
    assert first_manifest["output_tables"] == second_manifest["output_tables"]
    for table_name in FEATURE_TABLES:
        assert pq.read_table(first / table_name / "part-00000.parquet").equals(
            pq.read_table(second / table_name / "part-00000.parquet")
        )


def test_validation_detects_corrupt_feature_values(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    output = tmp_path / "features"
    build_features(dataset, output)
    path = output / "transaction_features" / "part-00000.parquet"
    table = pq.read_table(path)
    fee_index = table.schema.get_field_index("fee_sats")
    corrupted = table.set_column(
        fee_index,
        table.schema.field(fee_index),
        pa.array([-1, *table["fee_sats"].to_pylist()[1:]], type=pa.int64()),
    )
    pq.write_table(corrupted, path)

    report = validate_feature_store(output, dataset)
    assert not report.is_valid
    assert {issue.code for issue in report.issues} >= {
        "FEATURE_FILE_HASH_MISMATCH",
        "NEGATIVE_FEATURE_VALUE",
    }


def test_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    output = tmp_path / "features"
    output.mkdir()
    marker = output / "preserve.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FeatureBuildError, match="will not be overwritten"):
        build_features(dataset, output)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_definition_registry_matches_every_parquet_column() -> None:
    registry = build_definition_registry()
    defined = {(feature["table"], feature["name"]) for feature in registry["features"]}
    expected = {
        (table_name, field.name)
        for table_name, table in FEATURE_TABLES.items()
        for field in table.schema
    }
    assert defined == expected


def test_graph_projection_is_repeatable_and_cleans_temporary_tables(tmp_path: Path) -> None:
    dataset_path = create_feature_dataset(tmp_path)
    with AnalyticalDataset(dataset_path).connect() as connection:
        _register_scoped_views(connection, FeatureBuildConfig())
        first = build_component_size_table(connection)
        remaining = connection.execute(
            """SELECT count(*) FROM information_schema.tables
            WHERE table_name LIKE 'feature_graph_%'"""
        ).fetchone()
        second = build_component_size_table(connection)

    assert remaining == (0,)
    assert first.equals(second)
