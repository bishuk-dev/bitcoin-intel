from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bitcoin_intel.ingestion.cli import main as cli_main
from bitcoin_intel.ingestion.errors import ErrorCode, IngestionFileError
from bitcoin_intel.ingestion.parquet import PART_FILE_NAME, TABLE_DEFINITIONS
from bitcoin_intel.ingestion.pipeline import ingest_file
from tests.factories import make_source_record

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "equivalent"


@pytest.mark.parametrize("file_name", ["records.csv", "records.json", "records.xml"])
def test_end_to_end_ingestion_writes_verified_canonical_parquet(
    file_name: str, tmp_path: Path
) -> None:
    output = tmp_path / "dataset"
    summary = ingest_file(FIXTURE_DIRECTORY / file_name, output)

    assert summary.records_read == 2
    assert summary.records_accepted == 2
    assert summary.records_rejected == 0
    assert summary.unique_transactions == 1
    assert summary.network_observations == 2

    transactions = _read_table(output, "transactions")
    inputs = _read_table(output, "transaction_inputs")
    outputs = _read_table(output, "transaction_outputs")
    observations = _read_table(output, "network_observations")
    sources = _read_table(output, "source_records")

    assert transactions.num_rows == 1
    assert transactions.column("fee_sats").to_pylist() == [1_000_000]
    assert inputs.column("amount_sats").to_pylist() == [100_000_000, 50_000_000]
    assert outputs.column("amount_sats").to_pylist() == [140_000_000, 9_000_000]
    assert set(observations.column("src_ip").to_pylist()) == {"192.0.2.1", "2001:db8::1"}
    assert observations.schema.field("observed_at").type == pa.timestamp("us", tz="UTC")
    assert len(set(sources.column("source_record_id").to_pylist())) == 2

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["records_read"] == 2
    assert manifest["output_tables"]["network_observations"]["rows"] == 2

    for table_name, definition in TABLE_DEFINITIONS.items():
        table = _read_table(output, table_name)
        assert table.schema.equals(definition.arrow_schema, check_metadata=False)
        assert not any(pa.types.is_floating(field.type) for field in table.schema)


def test_all_formats_have_equivalent_canonical_semantics(tmp_path: Path) -> None:
    outputs: dict[str, Path] = {}
    for extension in ("csv", "json", "xml"):
        output = tmp_path / extension
        ingest_file(FIXTURE_DIRECTORY / f"records.{extension}", output)
        outputs[extension] = output

    reference = _semantic_tables(outputs["json"])
    assert _semantic_tables(outputs["csv"]) == reference
    assert _semantic_tables(outputs["xml"]) == reference


def test_repeated_ingestion_is_semantically_deterministic(tmp_path: Path) -> None:
    source = FIXTURE_DIRECTORY / "records.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    ingest_file(source, first)
    ingest_file(source, second)

    for table_name in TABLE_DEFINITIONS:
        assert _read_table(first, table_name).equals(_read_table(second, table_name))
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()


def test_conflicting_txid_definition_is_quarantined(tmp_path: Path) -> None:
    source = tmp_path / "conflict.json"
    first = make_source_record()
    second = make_source_record(
        timestamp="2026-08-28T12:31:00Z",
        output_amounts=["2"],
        fee="0",
    )
    _write_json(source, [first, second])

    output = tmp_path / "dataset"
    summary = ingest_file(source, output)

    assert summary.records_accepted == 1
    assert summary.records_rejected == 1
    assert _read_table(output, "transactions").num_rows == 1
    assert _read_table(output, "network_observations").num_rows == 1
    rejected = _read_table(output, "rejected_records").to_pylist()
    assert rejected[0]["record_index"] == 1
    assert rejected[0]["error_code"] == ErrorCode.TXID_CONTENT_CONFLICT.value


def test_invalid_record_is_rejected_without_losing_valid_neighbors(tmp_path: Path) -> None:
    source = tmp_path / "mixed.json"
    records = [
        make_source_record(txid="a" * 64),
        make_source_record(txid="invalid"),
        make_source_record(txid="b" * 64, timestamp="2026-08-28T12:32:00Z"),
    ]
    _write_json(source, records)

    output = tmp_path / "dataset"
    summary = ingest_file(source, output)

    assert (summary.records_read, summary.records_accepted, summary.records_rejected) == (3, 2, 1)
    assert _read_table(output, "transactions").num_rows == 2
    assert _read_table(output, "source_records").num_rows == 3
    rejected = _read_table(output, "rejected_records").to_pylist()
    assert rejected == [
        {
            "source_record_id": rejected[0]["source_record_id"],
            "source_file": "mixed.json",
            "record_index": 1,
            "error_code": ErrorCode.INVALID_TXID.value,
            "error_message": "txid must contain exactly 64 hexadecimal characters",
            "field_name": "txid",
        }
    ]


def test_malformed_csv_array_is_a_record_level_rejection(tmp_path: Path) -> None:
    source = tmp_path / "bad-array.csv"
    source.write_text(
        "timestamp,src_ip,dst_ip,src_port,dst_port,txid,input_addresses,"
        "output_addresses,input_amounts,output_amounts,fee\n"
        f"2026-08-28T12:30:00Z,192.0.2.1,198.51.100.2,8333,49152,{'a' * 64},"
        'not-json,"[""OutputAddressA""]","[1]","[0.9]",0.1\n',
        encoding="utf-8",
    )
    output = tmp_path / "dataset"

    summary = ingest_file(source, output)

    assert summary.records_rejected == 1
    rejected = _read_table(output, "rejected_records").to_pylist()
    assert rejected[0]["error_code"] == ErrorCode.MALFORMED_ARRAY.value


@pytest.mark.parametrize(
    ("file_name", "content"),
    [
        ("broken.json", '[{"txid": "abc"}'),
        ("broken.xml", "<records><record></records>"),
        ("unexpected-text.xml", "<records>not-whitespace</records>"),
        ("broken.csv", "txid,fee\nabc,1\n"),
        (
            "unsafe.xml",
            '<!DOCTYPE records [<!ENTITY payload "unsafe">]><records></records>',
        ),
    ],
)
def test_file_level_failure_does_not_publish_partial_dataset(
    file_name: str, content: str, tmp_path: Path
) -> None:
    source = tmp_path / file_name
    source.write_text(content, encoding="utf-8")
    output = tmp_path / "dataset"

    with pytest.raises(IngestionFileError):
        ingest_file(source, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".dataset.tmp-*"))


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "dataset"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve me", encoding="utf-8")

    with pytest.raises(IngestionFileError, match="will not be overwritten"):
        ingest_file(FIXTURE_DIRECTORY / "records.json", output)

    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_cli_reports_summary_without_dumping_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "dataset"
    exit_code = cli_main(
        [
            "ingest",
            "--input",
            str(FIXTURE_DIRECTORY / "records.json"),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Records read: 2" in captured.out
    assert "Unique transactions: 1" in captured.out
    assert "InputAddressA" not in captured.out


def test_cli_returns_nonzero_for_file_level_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "broken.json"
    source.write_text('[{"txid": "unfinished"}', encoding="utf-8")
    output = tmp_path / "dataset"

    exit_code = cli_main(["ingest", "--input", str(source), "--output", str(output)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Ingestion failed:" in captured.err
    assert not output.exists()


def _read_table(dataset: Path, table_name: str) -> pa.Table:
    return pq.read_table(dataset / table_name / PART_FILE_NAME)


def _semantic_tables(dataset: Path) -> dict[str, list[dict[str, Any]]]:
    result = {
        table_name: _read_table(dataset, table_name).to_pylist()
        for table_name in ("transactions", "transaction_inputs", "transaction_outputs")
    }
    observations = _read_table(dataset, "network_observations").drop(
        ["observation_id", "source_record_id"]
    )
    result["network_observations"] = sorted(
        observations.to_pylist(), key=lambda row: row["observed_at"]
    )
    return result


def _write_json(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
