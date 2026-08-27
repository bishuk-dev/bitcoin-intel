from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from bitcoin_intel.ingestion.errors import IngestionFileError
from bitcoin_intel.ingestion.normalization import CanonicalDataset

SCHEMA_VERSION = "1.0.0"
PART_FILE_NAME = "part-00000.parquet"


@dataclass(frozen=True, slots=True)
class TableDefinition:
    arrow_schema: pa.Schema
    polars_schema: pl.Schema
    sort_by: tuple[str, ...]


def _polars_schema(
    fields: Mapping[str, pl.DataType | type[pl.DataType]],
) -> pl.Schema:
    """Give Polars' overload the contextual type needed for mixed dtype forms."""

    return pl.Schema(fields)


TABLE_DEFINITIONS: dict[str, TableDefinition] = {
    "transactions": TableDefinition(
        pa.schema(
            [
                pa.field("txid", pa.string(), nullable=False),
                pa.field("fee_sats", pa.int64(), nullable=False),
                pa.field("script_type", pa.string()),
            ]
        ),
        _polars_schema({"txid": pl.String, "fee_sats": pl.Int64, "script_type": pl.String}),
        ("txid",),
    ),
    "transaction_inputs": TableDefinition(
        pa.schema(
            [
                pa.field("txid", pa.string(), nullable=False),
                pa.field("input_index", pa.int64(), nullable=False),
                pa.field("address", pa.string(), nullable=False),
                pa.field("amount_sats", pa.int64(), nullable=False),
            ]
        ),
        _polars_schema(
            {
                "txid": pl.String,
                "input_index": pl.Int64,
                "address": pl.String,
                "amount_sats": pl.Int64,
            }
        ),
        ("txid", "input_index"),
    ),
    "transaction_outputs": TableDefinition(
        pa.schema(
            [
                pa.field("txid", pa.string(), nullable=False),
                pa.field("output_index", pa.int64(), nullable=False),
                pa.field("address", pa.string(), nullable=False),
                pa.field("amount_sats", pa.int64(), nullable=False),
            ]
        ),
        _polars_schema(
            {
                "txid": pl.String,
                "output_index": pl.Int64,
                "address": pl.String,
                "amount_sats": pl.Int64,
            }
        ),
        ("txid", "output_index"),
    ),
    "network_observations": TableDefinition(
        pa.schema(
            [
                pa.field("observation_id", pa.string(), nullable=False),
                pa.field("txid", pa.string(), nullable=False),
                pa.field("observed_at", pa.timestamp("us", tz="UTC"), nullable=False),
                pa.field("src_ip", pa.string(), nullable=False),
                pa.field("dst_ip", pa.string(), nullable=False),
                pa.field("src_port", pa.int64(), nullable=False),
                pa.field("dst_port", pa.int64(), nullable=False),
                pa.field("reported_geo_country", pa.string()),
                pa.field("reported_asn", pa.int64()),
                pa.field("source_record_id", pa.string(), nullable=False),
            ]
        ),
        _polars_schema(
            {
                "observation_id": pl.String,
                "txid": pl.String,
                "observed_at": pl.Datetime("us", "UTC"),
                "src_ip": pl.String,
                "dst_ip": pl.String,
                "src_port": pl.Int64,
                "dst_port": pl.Int64,
                "reported_geo_country": pl.String,
                "reported_asn": pl.Int64,
                "source_record_id": pl.String,
            }
        ),
        ("observation_id",),
    ),
    "transaction_sources": TableDefinition(
        pa.schema(
            [
                pa.field("txid", pa.string(), nullable=False),
                pa.field("source_record_id", pa.string(), nullable=False),
            ]
        ),
        _polars_schema({"txid": pl.String, "source_record_id": pl.String}),
        ("txid", "source_record_id"),
    ),
    "source_records": TableDefinition(
        pa.schema(
            [
                pa.field("source_record_id", pa.string(), nullable=False),
                pa.field("source_file", pa.string(), nullable=False),
                pa.field("source_format", pa.string(), nullable=False),
                pa.field("source_file_sha256", pa.string(), nullable=False),
                pa.field("record_index", pa.int64(), nullable=False),
            ]
        ),
        _polars_schema(
            {
                "source_record_id": pl.String,
                "source_file": pl.String,
                "source_format": pl.String,
                "source_file_sha256": pl.String,
                "record_index": pl.Int64,
            }
        ),
        ("record_index",),
    ),
    "rejected_records": TableDefinition(
        pa.schema(
            [
                pa.field("source_record_id", pa.string(), nullable=False),
                pa.field("source_file", pa.string(), nullable=False),
                pa.field("record_index", pa.int64(), nullable=False),
                pa.field("error_code", pa.string(), nullable=False),
                pa.field("error_message", pa.string(), nullable=False),
                pa.field("field_name", pa.string()),
            ]
        ),
        _polars_schema(
            {
                "source_record_id": pl.String,
                "source_file": pl.String,
                "record_index": pl.Int64,
                "error_code": pl.String,
                "error_message": pl.String,
                "field_name": pl.String,
            }
        ),
        ("record_index",),
    ),
}


def write_canonical_dataset(
    output_directory: Path,
    dataset: CanonicalDataset,
    manifest_fields: Mapping[str, Any],
) -> dict[str, Any]:
    output_tables: dict[str, dict[str, Any]] = {}
    expected_counts: dict[str, int] = {}
    for table_name, definition in TABLE_DEFINITIONS.items():
        rows = cast_rows(getattr(dataset, table_name))
        expected_counts[table_name] = len(rows)
        table_path = output_directory / table_name / PART_FILE_NAME
        table_path.parent.mkdir(parents=True, exist_ok=False)
        arrow_table = _build_arrow_table(rows, definition)
        pq.write_table(
            arrow_table,
            table_path,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
            row_group_size=65_536,
            version="2.6",
        )
        output_tables[table_name] = {
            "file": f"{table_name}/{PART_FILE_NAME}",
            "rows": arrow_table.num_rows,
            "bytes": table_path.stat().st_size,
            "sha256": _sha256_file(table_path),
        }

    verify_canonical_dataset(output_directory, expected_counts)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        **manifest_fields,
        "output_tables": output_tables,
    }
    (output_directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_canonical_dataset(
    output_directory: Path, expected_counts: Mapping[str, int] | None = None
) -> None:
    loaded: dict[str, pa.Table] = {}
    for table_name, definition in TABLE_DEFINITIONS.items():
        table_path = output_directory / table_name / PART_FILE_NAME
        if not table_path.is_file():
            raise IngestionFileError(f"canonical table is missing: {table_name}")
        table = pq.read_table(table_path)
        if not table.schema.equals(definition.arrow_schema, check_metadata=False):
            raise IngestionFileError(
                f"canonical table {table_name} has an unexpected Parquet schema"
            )
        if expected_counts is not None and table.num_rows != expected_counts[table_name]:
            raise IngestionFileError(f"canonical table {table_name} failed row-count verification")
        if any(pa.types.is_floating(field.type) for field in table.schema):
            raise IngestionFileError(
                f"canonical table {table_name} contains a floating-point column"
            )
        loaded[table_name] = table

    _verify_relational_invariants(loaded)


def cast_rows(rows: Sequence[object]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if not is_dataclass(row) or isinstance(row, type):
            raise TypeError("canonical rows must be dataclass instances")
        result.append(asdict(row))
    return result


def _build_arrow_table(rows: list[dict[str, Any]], definition: TableDefinition) -> pa.Table:
    frame = pl.DataFrame(rows, schema=definition.polars_schema, orient="row", strict=True)
    if frame.height:
        frame = frame.sort(list(definition.sort_by))
    return frame.to_arrow().cast(definition.arrow_schema, safe=True)


def _verify_relational_invariants(tables: Mapping[str, pa.Table]) -> None:
    transactions = set(tables["transactions"].column("txid").to_pylist())
    source_records = set(tables["source_records"].column("source_record_id").to_pylist())
    _require_unique(tables["transactions"], ("txid",))
    _require_unique(tables["transaction_inputs"], ("txid", "input_index"))
    _require_unique(tables["transaction_outputs"], ("txid", "output_index"))
    _require_unique(tables["network_observations"], ("observation_id",))
    _require_unique(tables["transaction_sources"], ("txid", "source_record_id"))
    _require_unique(tables["source_records"], ("source_record_id",))

    for table_name in ("transaction_inputs", "transaction_outputs", "network_observations"):
        if not set(tables[table_name].column("txid").to_pylist()) <= transactions:
            raise IngestionFileError(f"canonical table {table_name} has an unknown txid")
    if not set(tables["transaction_sources"].column("txid").to_pylist()) <= transactions:
        raise IngestionFileError("transaction_sources has an unknown txid")
    for table_name in ("network_observations", "transaction_sources", "rejected_records"):
        if not set(tables[table_name].column("source_record_id").to_pylist()) <= source_records:
            raise IngestionFileError(
                f"canonical table {table_name} has an unknown source_record_id"
            )


def _require_unique(table: pa.Table, columns: tuple[str, ...]) -> None:
    keys_list = [
        tuple(table.column(column)[row_index].as_py() for column in columns)
        for row_index in range(table.num_rows)
    ]
    if len(keys_list) != len(set(keys_list)):
        raise IngestionFileError(f"canonical key is not unique: {', '.join(columns)}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
