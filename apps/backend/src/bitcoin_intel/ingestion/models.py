from __future__ import annotations

from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any, Never, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from bitcoin_intel.ingestion.errors import ErrorCode, MoneyConversionError, RecordIssue
from bitcoin_intel.ingestion.validation import btc_to_satoshis

SOURCE_FIELDS = frozenset(
    {
        "timestamp",
        "src_ip",
        "dst_ip",
        "src_port",
        "dst_port",
        "txid",
        "input_addresses",
        "output_addresses",
        "input_amounts",
        "output_amounts",
        "fee",
        "script_type",
        "geo_country",
        "asn",
    }
)
REQUIRED_SOURCE_FIELDS = SOURCE_FIELDS - {"script_type", "geo_country", "asn"}
ARRAY_SOURCE_FIELDS = frozenset(
    {"input_addresses", "output_addresses", "input_amounts", "output_amounts"}
)

_SOURCE_NAME_BY_MODEL_FIELD = {
    "observed_at": "timestamp",
    "input_amounts_sats": "input_amounts",
    "output_amounts_sats": "output_amounts",
    "fee_sats": "fee",
    "reported_geo_country": "geo_country",
    "reported_asn": "asn",
}


class ValidatedInputRecord(BaseModel):
    """One source record after shared semantic validation and normalization."""

    model_config = ConfigDict(extra="forbid", populate_by_name=False)

    observed_at: datetime = Field(validation_alias="timestamp")
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    txid: str
    input_addresses: list[str]
    output_addresses: list[str]
    input_amounts_sats: list[int] = Field(validation_alias="input_amounts")
    output_amounts_sats: list[int] = Field(validation_alias="output_amounts")
    fee_sats: int = Field(validation_alias="fee")
    script_type: str | None = None
    reported_geo_country: str | None = Field(default=None, validation_alias="geo_country")
    reported_asn: int | None = Field(default=None, validation_alias="asn")

    @field_validator("observed_at", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any) -> datetime:
        if not isinstance(value, str):
            _raise(ErrorCode.INVALID_TIMESTAMP, "timestamp must be an ISO-8601 string")
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            _raise(ErrorCode.INVALID_TIMESTAMP, "timestamp is not valid ISO-8601")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _raise(ErrorCode.INVALID_TIMESTAMP, "timestamp must include an explicit timezone")
        return parsed.astimezone(UTC)

    @field_validator("src_ip", "dst_ip", mode="before")
    @classmethod
    def normalize_ip(cls, value: Any) -> str:
        if not isinstance(value, str):
            _raise(ErrorCode.INVALID_IP, "IP address must be a string")
        try:
            return str(ip_address(value.strip()))
        except ValueError:
            _raise(ErrorCode.INVALID_IP, "IP address is not valid IPv4 or IPv6")

    @field_validator("src_port", "dst_port", mode="before")
    @classmethod
    def validate_port(cls, value: Any) -> int:
        parsed = _parse_decimal_integer(value, ErrorCode.INVALID_PORT, "port")
        if not 0 <= parsed <= 65_535:
            _raise(ErrorCode.INVALID_PORT, "port must be between 0 and 65535")
        return parsed

    @field_validator("txid", mode="before")
    @classmethod
    def normalize_txid(cls, value: Any) -> str:
        if not isinstance(value, str):
            _raise(ErrorCode.INVALID_TXID, "txid must be a string")
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            _raise(ErrorCode.INVALID_TXID, "txid must contain exactly 64 hexadecimal characters")
        return normalized

    @field_validator("input_addresses", "output_addresses", mode="before")
    @classmethod
    def validate_addresses(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            _raise(ErrorCode.MALFORMED_ARRAY, "address field must be an array")
        normalized: list[str] = []
        for address in value:
            if not isinstance(address, str):
                _raise(ErrorCode.INVALID_ADDRESS, "address values must be strings")
            trimmed = address.strip()
            if (
                not trimmed
                or len(trimmed) > 256
                or any(character.isspace() or ord(character) < 32 for character in trimmed)
            ):
                _raise(ErrorCode.INVALID_ADDRESS, "address is empty or obviously malformed")
            normalized.append(trimmed)
        return normalized

    @field_validator("input_amounts_sats", "output_amounts_sats", mode="before")
    @classmethod
    def normalize_amounts(cls, value: Any) -> list[int]:
        if not isinstance(value, list):
            _raise(ErrorCode.MALFORMED_ARRAY, "amount field must be an array")
        return [_money_to_satoshis(amount) for amount in value]

    @field_validator("fee_sats", mode="before")
    @classmethod
    def normalize_fee(cls, value: Any) -> int:
        return _money_to_satoshis(value)

    @field_validator("script_type", mode="before")
    @classmethod
    def normalize_script_type(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            _raise(ErrorCode.MALFORMED_SOURCE_RECORD, "script_type must be a string or null")
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > 128 or any(ord(character) < 32 for character in normalized):
            _raise(ErrorCode.MALFORMED_SOURCE_RECORD, "script_type is malformed")
        return normalized

    @field_validator("reported_geo_country", mode="before")
    @classmethod
    def normalize_country(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            _raise(ErrorCode.INVALID_COUNTRY, "geo_country must be a string or null")
        normalized = value.strip().upper()
        if len(normalized) != 2 or not normalized.isascii() or not normalized.isalpha():
            _raise(ErrorCode.INVALID_COUNTRY, "geo_country must be a two-letter code")
        return normalized

    @field_validator("reported_asn", mode="before")
    @classmethod
    def normalize_asn(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        parsed = _parse_decimal_integer(value, ErrorCode.INVALID_ASN, "asn")
        if not 0 <= parsed <= 4_294_967_295:
            _raise(ErrorCode.INVALID_ASN, "asn must be between 0 and 4294967295")
        return parsed

    @model_validator(mode="after")
    def validate_cardinality(self) -> Self:
        if len(self.input_addresses) != len(self.input_amounts_sats):
            _raise(
                ErrorCode.INPUT_CARDINALITY_MISMATCH,
                "input_addresses and input_amounts must contain the same number of items",
            )
        if len(self.output_addresses) != len(self.output_amounts_sats):
            _raise(
                ErrorCode.OUTPUT_CARDINALITY_MISMATCH,
                "output_addresses and output_amounts must contain the same number of items",
            )
        return self


def validation_error_to_issue(error: ValidationError) -> RecordIssue:
    first_error = error.errors(include_url=False, include_input=False)[0]
    raw_code = str(first_error["type"])
    try:
        code = ErrorCode(raw_code)
    except ValueError:
        code = ErrorCode.MALFORMED_SOURCE_RECORD
    location = first_error.get("loc", ())
    field_name: str | None = None
    if location:
        model_field = str(location[0])
        field_name = _SOURCE_NAME_BY_MODEL_FIELD.get(model_field, model_field)
    return RecordIssue(code=code, message=str(first_error["msg"]), field_name=field_name)


def _money_to_satoshis(value: Any) -> int:
    try:
        return btc_to_satoshis(value)
    except MoneyConversionError as error:
        _raise(error.code, str(error))


def _parse_decimal_integer(value: Any, code: ErrorCode, label: str) -> int:
    if isinstance(value, bool):
        _raise(code, f"{label} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    _raise(code, f"{label} must be an integer")


def _raise(code: ErrorCode, message: str) -> Never:
    raise PydanticCustomError(code.value, message)
