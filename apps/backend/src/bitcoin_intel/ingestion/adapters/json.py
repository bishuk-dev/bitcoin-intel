from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO, cast

from bitcoin_intel.ingestion.adapters.base import MAX_SOURCE_FIELD_SIZE, RawInputRecord
from bitcoin_intel.ingestion.errors import ErrorCode, IngestionFileError, RecordIssue

_READ_CHUNK_SIZE = 65_536


def iter_json_records(path: Path) -> Iterator[RawInputRecord]:
    decoder = json.JSONDecoder(
        parse_float=Decimal,
        parse_int=int,
        parse_constant=lambda token: token,
    )
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            yield from _JsonArrayStream(source, decoder).iter_records()
    except UnicodeDecodeError as error:
        raise IngestionFileError(
            f"JSON input is not valid UTF-8 near byte {error.start}"
        ) from error
    except OSError as error:
        raise IngestionFileError(f"failed to read JSON input: {error}") from error


class _JsonArrayStream:
    def __init__(self, source: TextIO, decoder: json.JSONDecoder) -> None:
        self._source = source
        self._decoder = decoder
        self._buffer = ""
        self._position = 0
        self._eof = False

    def iter_records(self) -> Iterator[RawInputRecord]:
        self._skip_whitespace()
        if self._peek() != "[":
            raise IngestionFileError("JSON input must contain one top-level array")
        self._position += 1
        self._skip_whitespace()
        if self._peek() == "]":
            self._position += 1
            self._assert_finished()
            return

        record_index = 0
        while True:
            value = self._decode_value(record_index)
            if isinstance(value, Mapping):
                yield RawInputRecord(record_index, cast(Mapping[str, Any], value))
            else:
                yield RawInputRecord(
                    record_index,
                    None,
                    RecordIssue(
                        ErrorCode.MALFORMED_SOURCE_RECORD,
                        "JSON array entries must be objects",
                    ),
                )
            record_index += 1
            self._skip_whitespace()
            delimiter = self._peek()
            if delimiter == "]":
                self._position += 1
                self._assert_finished()
                return
            if delimiter != ",":
                raise IngestionFileError(
                    f"JSON array requires ',' or ']' after record {record_index - 1}"
                )
            self._position += 1
            self._skip_whitespace()

    def _decode_value(self, record_index: int) -> Any:
        record_start = self._position
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._position)
            except json.JSONDecodeError as error:
                if self._eof:
                    raise IngestionFileError(
                        f"JSON is malformed near record {record_index}: {error.msg}"
                    ) from error
                if len(self._buffer) - record_start > MAX_SOURCE_FIELD_SIZE:
                    raise IngestionFileError(
                        f"JSON record {record_index} exceeds the size safety limit"
                    ) from error
                self._read_more(compact=False)
                continue
            if end - record_start > MAX_SOURCE_FIELD_SIZE:
                raise IngestionFileError(
                    f"JSON record {record_index} exceeds the size safety limit"
                )
            self._position = end
            return value

    def _skip_whitespace(self) -> None:
        while True:
            while self._position < len(self._buffer) and self._buffer[self._position].isspace():
                self._position += 1
            if self._position < len(self._buffer) or self._eof:
                return
            self._read_more(compact=True)

    def _peek(self) -> str | None:
        if self._position >= len(self._buffer) and not self._eof:
            self._read_more(compact=True)
        if self._position >= len(self._buffer):
            return None
        return self._buffer[self._position]

    def _read_more(self, *, compact: bool) -> None:
        if compact and self._position:
            self._buffer = self._buffer[self._position :]
            self._position = 0
        chunk = self._source.read(_READ_CHUNK_SIZE)
        if chunk:
            self._buffer += chunk
        else:
            self._eof = True

    def _assert_finished(self) -> None:
        self._skip_whitespace()
        if self._peek() is not None:
            raise IngestionFileError("JSON input contains data after the top-level array")
