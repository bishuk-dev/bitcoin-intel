from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.graph.constants import GRAPH_SCHEMA_VERSION, NEO4J_VERSION
from bitcoin_intel.graph.import_builder import (
    IMPORT_DEFINITIONS,
    GraphImportError,
    prepare_graph_import,
    validate_graph_import,
)


def test_graph_import_contains_exact_factual_nodes_and_relationships(
    analytical_dataset_path: Path, tmp_path: Path
) -> None:
    prepared = prepare_graph_import(analytical_dataset_path, tmp_path / "graph-import")
    manifest = prepared.manifest

    assert manifest.graph_schema_version == GRAPH_SCHEMA_VERSION
    assert manifest.canonical_schema_version == "1.0.0"
    assert manifest.neo4j_version == NEO4J_VERSION
    assert manifest.node_counts == {
        "transactions": 3,
        "addresses": 6,
        "ip_addresses": 5,
        "network_observations": 4,
    }
    assert manifest.relationship_counts == {
        "spent_in": 5,
        "created_output": 5,
        "observed_transaction": 4,
        "source_ip": 4,
        "destination_ip": 4,
    }

    transactions = _rows(prepared.path, "transaction_nodes.parquet")
    addresses = _rows(prepared.path, "address_nodes.parquet")
    ip_addresses = _rows(prepared.path, "ip_address_nodes.parquet")
    observations = _rows(prepared.path, "network_observation_nodes.parquet")
    spent_in = _rows(prepared.path, "spent_in_relationships.parquet")
    created_output = _rows(prepared.path, "created_output_relationships.parquet")

    assert [row["txid"] for row in transactions] == ["a" * 64, "b" * 64, "c" * 64]
    assert transactions[-1]["script_type"] is None
    assert {row["address"] for row in addresses} == {
        "InputOnly",
        "SharedMulti",
        "OutputOnly",
        "BothAddress",
        "SecondOutput",
        "ThirdOutput",
    }
    assert sum(row["address"] == "BothAddress" for row in addresses) == 1
    assert {row["ip"] for row in ip_addresses} == {
        "192.0.2.1",
        "198.51.100.2",
        "203.0.113.9",
        "2001:db8::1",
        "2001:db8::2",
    }
    assert sum(row["ip"] == "192.0.2.1" for row in ip_addresses) == 1
    assert observations[1]["reported_geo_country"] is None
    assert observations[1]["reported_asn"] is None
    assert all(row["observed_at"].utcoffset().total_seconds() == 0 for row in observations)

    # The same Address is both an output of A and an input/output of B, without ownership claims.
    assert {
        (row["address"], row["txid"], row["input_index"], row["amount_sats"])
        for row in spent_in
        if row["address"] == "BothAddress"
    } == {("BothAddress", "b" * 64, 0, 100_000_000)}
    assert {
        (row["txid"], row["address"], row["output_index"], row["amount_sats"])
        for row in created_output
        if row["address"] == "BothAddress"
    } == {
        ("a" * 64, "BothAddress", 1, 240_000_000),
        ("b" * 64, "BothAddress", 0, 50_000_000),
    }

    observed = _rows(prepared.path, "observed_transaction_relationships.parquet")
    assert sum(row["txid"] == "a" * 64 for row in observed) == 2
    assert len({row["observation_id"] for row in observed}) == 4
    assert len(_rows(prepared.path, "source_ip_relationships.parquet")) == 4
    assert len(_rows(prepared.path, "destination_ip_relationships.parquet")) == 4

    report = validate_graph_import(
        prepared.path, canonical_dataset=AnalyticalDataset(analytical_dataset_path)
    )
    assert report.is_valid


def test_graph_import_uses_explicit_arrow_schemas(
    analytical_dataset_path: Path, tmp_path: Path
) -> None:
    output = prepare_graph_import(analytical_dataset_path, tmp_path / "graph-import").path

    for definition in IMPORT_DEFINITIONS:
        actual = pq.read_schema(output / definition.file_name)
        assert actual.equals(definition.schema, check_metadata=False)
        assert not any(pa.types.is_floating(field.type) for field in actual)


def test_repeated_graph_import_build_is_physically_deterministic_except_timestamp(
    analytical_dataset_path: Path, tmp_path: Path
) -> None:
    first = prepare_graph_import(analytical_dataset_path, tmp_path / "first").manifest
    second = prepare_graph_import(analytical_dataset_path, tmp_path / "second").manifest

    assert first.node_counts == second.node_counts
    assert first.relationship_counts == second.relationship_counts
    assert first.canonical_manifest_sha256 == second.canonical_manifest_sha256
    assert {
        name: (item.sha256, item.header_sha256, item.rows) for name, item in first.files.items()
    } == {name: (item.sha256, item.header_sha256, item.rows) for name, item in second.files.items()}


def test_graph_import_refuses_to_overwrite_existing_destination(
    analytical_dataset_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "graph-import"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(GraphImportError, match="already exists"):
        prepare_graph_import(analytical_dataset_path, output)

    assert marker.read_text(encoding="utf-8") == "user data"


def test_graph_import_detects_tampered_parquet(
    analytical_dataset_path: Path, tmp_path: Path
) -> None:
    output = prepare_graph_import(analytical_dataset_path, tmp_path / "graph-import").path
    transaction_file = output / "transaction_nodes.parquet"
    with transaction_file.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(GraphImportError, match=r"cannot read transaction_nodes\.parquet"):
        validate_graph_import(output)


def test_graph_import_detects_duplicate_node_and_orphan_relationship(
    analytical_dataset_path: Path, tmp_path: Path
) -> None:
    output = prepare_graph_import(analytical_dataset_path, tmp_path / "graph-import").path
    address_file = output / "address_nodes.parquet"
    address_table = pq.read_table(address_file)
    pq.write_table(
        pa.concat_tables([address_table, address_table.slice(0, 1)]),
        address_file,
        compression="zstd",
        version="2.6",
    )
    _refresh_manifest_file(output, "address_nodes.parquet")

    relationship_file = output / "spent_in_relationships.parquet"
    relationship_table = pq.read_table(relationship_file)
    orphan = pa.Table.from_pylist(
        [
            {
                "address": "MissingAddress",
                "txid": "a" * 64,
                "input_index": 99,
                "amount_sats": 1,
            }
        ],
        schema=relationship_table.schema,
    )
    pq.write_table(
        pa.concat_tables([relationship_table, orphan]),
        relationship_file,
        compression="zstd",
        version="2.6",
    )
    _refresh_manifest_file(output, "spent_in_relationships.parquet")

    report = validate_graph_import(output)
    codes = {issue.code for issue in report.issues}

    assert "DUPLICATE_ADDRESS_ID" in codes
    assert "ORPHAN_SPENT_IN_ADDRESS" in codes


def _rows(root: Path, name: str) -> list[dict[str, Any]]:
    return list(pq.read_table(root / name).to_pylist())


def _refresh_manifest_file(root: Path, name: str) -> None:
    manifest_path = root / "graph-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    path = root / name
    entry = manifest["files"][name]
    entry["rows"] = pq.ParquetFile(path).metadata.num_rows
    entry["bytes"] = path.stat().st_size
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
