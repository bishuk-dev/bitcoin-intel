from __future__ import annotations

import csv
import json
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from bitcoin_intel.ingestion.adapters.base import MAX_SOURCE_FIELD_SIZE, RawInputRecord
from bitcoin_intel.ingestion.errors import ErrorCode, IngestionFileError, RecordIssue
from bitcoin_intel.ingestion.models import (
    ARRAY_SOURCE_FIELDS,
    REQUIRED_SOURCE_FIELDS,
    SOURCE_FIELDS,
)

_ORDERED_ARRAY_FIELDS = (
    "input_addresses",
    "output_addresses",
    "input_amounts",
    "output_amounts",
)


def iter_csv_records(path: Path) -> Iterator[RawInputRecord]:
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_SOURCE_FIELD_SIZE)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            _validate_header(reader.fieldnames)
            for record_index, row in enumerate(reader):
                if None in row:
                    yield RawInputRecord(
                        record_index,
                        None,
                        RecordIssue(
                            ErrorCode.MALFORMED_SOURCE_RECORD,
                            "CSV row contains more values than the header",
                        ),
                    )
                    continue
                data = {key: value for key, value in row.items()}
                issue = _parse_array_fields(data)
                yield RawInputRecord(record_index, data if issue is None else None, issue)
    except UnicodeDecodeError as error:
        raise IngestionFileError(f"CSV input is not valid UTF-8 near byte {error.start}") from error
    except csv.Error as error:
        raise IngestionFileError(f"CSV structure is malformed: {error}") from error
    except OSError as error:
        raise IngestionFileError(f"failed to read CSV input: {error}") from error
    finally:
        csv.field_size_limit(previous_limit)


def _validate_header(fieldnames: Sequence[str] | None) -> None:
    if fieldnames is None:
        raise IngestionFileError("CSV input has no header")
    if len(fieldnames) != len(set(fieldnames)):
        raise IngestionFileError("CSV header contains duplicate column names")
    fields = set(fieldnames)
    missing = sorted(REQUIRED_SOURCE_FIELDS - fields)
    unknown = sorted(fields - SOURCE_FIELDS)
    if missing:
        raise IngestionFileError(f"CSV header is missing required fields: {', '.join(missing)}")
    if unknown:
        raise IngestionFileError(f"CSV header contains unsupported fields: {', '.join(unknown)}")


def _parse_array_fields(data: dict[str, Any]) -> RecordIssue | None:
    for field_name in _ORDERED_ARRAY_FIELDS:
        if field_name not in ARRAY_SOURCE_FIELDS:
            continue
        raw_value = data.get(field_name)
        if not isinstance(raw_value, str):
            return RecordIssue(
                ErrorCode.MALFORMED_ARRAY,
                f"CSV field {field_name} must contain a JSON array",
                field_name,
            )
        try:
            parsed = json.loads(
                raw_value,
                parse_float=Decimal,
                parse_constant=lambda token: token,
            )
        except json.JSONDecodeError:
            return RecordIssue(
                ErrorCode.MALFORMED_ARRAY,
                f"CSV field {field_name} does not contain valid JSON",
                field_name,
            )
        if not isinstance(parsed, list):
            return RecordIssue(
                ErrorCode.MALFORMED_ARRAY,
                f"CSV field {field_name} must contain a JSON array",
                field_name,
            )
        data[field_name] = parsed
    return None
