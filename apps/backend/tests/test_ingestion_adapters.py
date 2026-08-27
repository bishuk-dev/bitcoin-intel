from __future__ import annotations

from pathlib import Path

import pytest

from bitcoin_intel.ingestion.adapters import detect_source_format, iter_source_records
from bitcoin_intel.ingestion.adapters.base import SourceFormat
from bitcoin_intel.ingestion.models import ValidatedInputRecord

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "equivalent"


@pytest.mark.parametrize(
    ("file_name", "source_format"),
    [
        ("records.csv", SourceFormat.CSV),
        ("records.json", SourceFormat.JSON),
        ("records.xml", SourceFormat.XML),
    ],
)
def test_format_adapters_produce_equivalent_validated_records(
    file_name: str, source_format: SourceFormat
) -> None:
    path = FIXTURE_DIRECTORY / file_name
    assert detect_source_format(path) is source_format

    raw_records = list(iter_source_records(path, source_format))
    assert [record.record_index for record in raw_records] == [0, 1]
    assert all(record.issue is None and record.data is not None for record in raw_records)

    validated = [
        ValidatedInputRecord.model_validate(record.data)
        for record in raw_records
        if record.data is not None
    ]
    reference_path = FIXTURE_DIRECTORY / "records.json"
    reference = [
        ValidatedInputRecord.model_validate(record.data)
        for record in iter_source_records(reference_path, SourceFormat.JSON)
        if record.data is not None
    ]
    assert validated == reference
