from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from bitcoin_intel.enrichment.pipeline import build_ip_enrichment
from bitcoin_intel.features.models import FeatureBuildConfig
from bitcoin_intel.features.pipeline import build_features, build_features_v1
from bitcoin_intel.features.validation import validate_feature_store
from bitcoin_intel.ml.dataset import load_experiment_dataset
from tests.enrichment_fixtures import write_test_mmdb_resources
from tests.feature_fixtures import create_feature_dataset, read_feature_rows


def _stores(tmp_path: Path) -> tuple[Path, Path]:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    enrichment = tmp_path / "enrichment"
    build_ip_enrichment(dataset, enrichment, country, asn)
    return dataset, enrichment


def test_v2_feature_math_and_lineage(tmp_path: Path) -> None:
    dataset, enrichment = _stores(tmp_path)
    output = tmp_path / "features-v2"
    build_features(dataset, output, enrichment)
    transactions = {row["txid"]: row for row in read_feature_rows(output, "transaction_features")}
    correlations = {
        row["address"]: row for row in read_feature_rows(output, "correlation_features")
    }
    tx1 = transactions["1" * 64]
    assert tx1["unique_enriched_country_count"] == 3
    assert tx1["unique_enriched_asn_count"] == 3
    assert tx1["source_destination_country_match_rate"] == 0.2
    assert tx1["source_destination_asn_match_rate"] == 0.2
    assert transactions["2" * 64]["source_destination_country_match_rate"] is None
    assert correlations["AddressA"]["associated_enriched_country_count"] == 3
    assert correlations["AddressA"]["associated_enriched_asn_count"] == 3
    assert correlations["AddressA"]["associated_cross_country_observation_count"] == 4
    assert correlations["AddressA"]["associated_cross_asn_observation_count"] == 4
    manifest = json.loads((output / "feature-manifest.json").read_text(encoding="utf-8"))
    assert manifest["feature_schema_version"] == "2.0.0"
    assert manifest["enrichment_dataset_id"]
    assert validate_feature_store(output, dataset, enrichment).is_valid


def test_v2_preserves_v1_base_math_and_reported_metadata(tmp_path: Path) -> None:
    dataset, enrichment = _stores(tmp_path)
    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    build_features_v1(dataset, v1)
    build_features(dataset, v2, enrichment)
    for table_name in (
        "transaction_features",
        "address_features",
        "ip_features",
        "correlation_features",
    ):
        v1_rows = read_feature_rows(v1, table_name)
        v2_rows = read_feature_rows(v2, table_name)
        v1_columns = set(v1_rows[0])
        assert [{key: row[key] for key in v1_columns} for row in v2_rows] == v1_rows
    ip_schema = pq.read_schema(v2 / "ip_features" / "part-00000.parquet")
    assert all(not name.startswith("enriched_") for name in ip_schema.names)


def test_cutoff_applies_before_enrichment_aggregation(tmp_path: Path) -> None:
    dataset, enrichment = _stores(tmp_path)
    output = tmp_path / "cutoff"
    build_features(
        dataset,
        output,
        enrichment,
        FeatureBuildConfig(cutoff=datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)),
    )
    tx1 = read_feature_rows(output, "transaction_features")[0]
    assert tx1["unique_enriched_country_count"] == 2
    assert tx1["source_destination_country_match_rate"] == 1 / 3


def test_phase5_loader_reads_both_feature_schema_versions(tmp_path: Path) -> None:
    dataset, enrichment = _stores(tmp_path)
    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    build_features_v1(dataset, v1)
    build_features(dataset, v2, enrichment)
    for store in (v1, v2):
        loaded = load_experiment_dataset(store, "transaction-only", None)
        assert loaded.values.shape[0] == 3
        assert all(
            "scenario" not in column and "truth" not in column for column in loaded.feature_columns
        )
