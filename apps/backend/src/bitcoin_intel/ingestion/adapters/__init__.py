from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from bitcoin_intel.ingestion.adapters.base import (
    RawInputRecord,
    SourceFormat,
    detect_source_format,
)
from bitcoin_intel.ingestion.adapters.csv import iter_csv_records
from bitcoin_intel.ingestion.adapters.json import iter_json_records
from bitcoin_intel.ingestion.adapters.xml import iter_xml_records

__all__ = ["RawInputRecord", "SourceFormat", "detect_source_format", "iter_source_records"]


def iter_source_records(path: Path, source_format: SourceFormat) -> Iterator[RawInputRecord]:
    if source_format is SourceFormat.CSV:
        yield from iter_csv_records(path)
    elif source_format is SourceFormat.JSON:
        yield from iter_json_records(path)
    else:
        yield from iter_xml_records(path)
