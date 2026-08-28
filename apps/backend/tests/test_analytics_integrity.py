from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bitcoin_intel.analytics import AnalyticalDataset, AnalyticalQueries
from bitcoin_intel.analytics.validation import validate_analytical_dataset
from bitcoin_intel.ingestion import ingest_file
from bitcoin_intel.ingestion.parquet import PART_FILE_NAME
from tests.analytics_fixtures import write_json_records
from tests.factories import make_source_record


def test_valid_dataset_passes_integrity_validation(analytical_dataset_path: Path) -> None:
    report = validate_analytical_dataset(AnalyticalDataset(analytical_dataset_path))

    assert report.is_valid
    assert report.issues == ()


@pytest.mark.parametrize(
    ("table_name", "column_name", "replacement", "expected_code"),
    [
        ("transaction_inputs", "txid", "f" * 64, "ORPHAN_TRANSACTION_INPUT"),
        ("transaction_inputs", "amount_sats", -1, "NEGATIVE_INPUT_AMOUNT"),
        ("transactions", "fee_sats", -1, "NEGATIVE_TRANSACTION_FEE"),
    ],
)
def test_integrity_validation_detects_corrupt_values(
    analytical_dataset_path: Path,
    table_name: str,
    column_name: str,
    replacement: Any,
    expected_code: str,
) -> None:
    _replace_first_value(analytical_dataset_path, table_name, column_name, replacement)

    report = validate_analytical_dataset(AnalyticalDataset(analytical_dataset_path))

    assert not report.is_valid
    assert expected_code in {issue.code for issue in report.issues}


def test_integrity_validation_detects_duplicate_keys_and_manifest_count_mismatch(
    analytical_dataset_path: Path,
) -> None:
    table_path = analytical_dataset_path / "transaction_inputs" / PART_FILE_NAME
    table = pq.read_table(table_path)
    pq.write_table(pa.concat_tables([table, table.slice(0, 1)]), table_path, compression="zstd")

    report = validate_analytical_dataset(AnalyticalDataset(analytical_dataset_path))
    codes = {issue.code for issue in report.issues}

    assert "DUPLICATE_TRANSACTION_INPUT" in codes
    assert "MANIFEST_ROW_COUNT_MISMATCH" in codes


def test_empty_dataset_queries_return_empty_results(tmp_path: Path) -> None:
    source = tmp_path / "empty.json"
    source.write_text("[]", encoding="utf-8")
    dataset_path = tmp_path / "dataset"
    ingest_file(source, dataset_path)
    dataset = AnalyticalDataset(dataset_path)

    with dataset.connect() as connection:
        queries = AnalyticalQueries(connection)
        assert queries.transaction_summaries() == ()
        assert queries.high_value_transactions() == ()
        assert queries.temporal_activity("day") == ()
        assert queries.address_activity("unused").transaction_count == 0
        assert queries.ip_activity("2001:db8::1").observation_count == 0
    assert validate_analytical_dataset(dataset).is_valid


def test_sum_promotes_beyond_int64_without_losing_satoshis(tmp_path: Path) -> None:
    input_count = 5_000
    record = make_source_record(
        txid="d" * 64,
        input_addresses=[f"LargeInput{index}" for index in range(input_count)],
        input_amounts=["21000000"] * input_count,
        output_addresses=[],
        output_amounts=[],
        fee="0",
    )
    source = tmp_path / "large-sum.json"
    write_json_records(source, [record])
    dataset_path = tmp_path / "dataset"
    ingest_file(source, dataset_path)

    with AnalyticalDataset(dataset_path).connect() as connection:
        summary = AnalyticalQueries(connection).transaction_summaries(limit=1)[0]
        sum_type = connection.execute(
            "SELECT typeof(sum(amount_sats)) FROM transaction_inputs"
        ).fetchone()

    assert summary.total_input_sats == 10_500_000_000_000_000_000
    assert summary.total_input_sats > 2**63 - 1
    assert sum_type == ("HUGEINT",)


def _replace_first_value(
    dataset_path: Path, table_name: str, column_name: str, replacement: Any
) -> None:
    table_path = dataset_path / table_name / PART_FILE_NAME
    table = pq.read_table(table_path)
    column_index = table.schema.get_field_index(column_name)
    values = table.column(column_index).to_pylist()
    values[0] = replacement
    replacement_column = pa.array(values, type=table.schema.field(column_index).type)
    updated = table.set_column(column_index, table.schema.field(column_index), replacement_column)
    pq.write_table(updated, table_path, compression="zstd")
