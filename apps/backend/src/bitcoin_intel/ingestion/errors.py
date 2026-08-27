from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_TXID = "INVALID_TXID"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_IP = "INVALID_IP"
    INVALID_PORT = "INVALID_PORT"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    AMOUNT_PRECISION_EXCEEDED = "AMOUNT_PRECISION_EXCEEDED"
    NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
    INVALID_ADDRESS = "INVALID_ADDRESS"
    INVALID_COUNTRY = "INVALID_COUNTRY"
    INVALID_ASN = "INVALID_ASN"
    INPUT_CARDINALITY_MISMATCH = "INPUT_CARDINALITY_MISMATCH"
    OUTPUT_CARDINALITY_MISMATCH = "OUTPUT_CARDINALITY_MISMATCH"
    MALFORMED_ARRAY = "MALFORMED_ARRAY"
    MALFORMED_SOURCE_RECORD = "MALFORMED_SOURCE_RECORD"
    TXID_CONTENT_CONFLICT = "TXID_CONTENT_CONFLICT"


@dataclass(frozen=True, slots=True)
class RecordIssue:
    code: ErrorCode
    message: str
    field_name: str | None = None


class MoneyConversionError(ValueError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class IngestionFileError(RuntimeError):
    """A file-level failure for which no successful dataset should be published."""
