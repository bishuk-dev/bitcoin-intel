from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from bitcoin_intel.ingestion.errors import IngestionFileError, RecordIssue

MAX_SOURCE_FIELD_SIZE = 1_048_576


class SourceFormat(StrEnum):
    CSV = "csv"
    JSON = "json"
    XML = "xml"


@dataclass(frozen=True, slots=True)
class RawInputRecord:
    record_index: int
    data: Mapping[str, Any] | None
    issue: RecordIssue | None = None


def detect_source_format(path: Path) -> SourceFormat:
    try:
        return {
            ".csv": SourceFormat.CSV,
            ".json": SourceFormat.JSON,
            ".xml": SourceFormat.XML,
        }[path.suffix.lower()]
    except KeyError as error:
        raise IngestionFileError(
            f"unsupported input extension {path.suffix!r}; expected .csv, .json, or .xml"
        ) from error
