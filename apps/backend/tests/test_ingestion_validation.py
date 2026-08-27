from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from bitcoin_intel.ingestion.errors import ErrorCode, MoneyConversionError
from bitcoin_intel.ingestion.models import ValidatedInputRecord, validation_error_to_issue
from bitcoin_intel.ingestion.validation import (
    MAX_BITCOIN_SATS,
    btc_to_satoshis,
    build_observation_id,
    build_source_record_id,
)
from tests.factories import make_source_record


@pytest.mark.parametrize(
    ("btc", "satoshis"),
    [
        ("1", 100_000_000),
        ("0.5", 50_000_000),
        ("0.00000001", 1),
        ("12.34567890", 1_234_567_890),
        (Decimal("0"), 0),
    ],
)
def test_btc_to_satoshis_exact_values(btc: object, satoshis: int) -> None:
    assert btc_to_satoshis(btc) == satoshis


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("0.000000001", ErrorCode.AMOUNT_PRECISION_EXCEEDED),
        ("-0.1", ErrorCode.NEGATIVE_AMOUNT),
        ("NaN", ErrorCode.INVALID_AMOUNT),
        ("Infinity", ErrorCode.INVALID_AMOUNT),
        (0.5, ErrorCode.INVALID_AMOUNT),
    ],
)
def test_btc_to_satoshis_rejects_invalid_values(value: object, code: ErrorCode) -> None:
    with pytest.raises(MoneyConversionError) as captured:
        btc_to_satoshis(value)

    assert captured.value.code is code


@given(st.integers(min_value=0, max_value=MAX_BITCOIN_SATS))
def test_satoshi_conversion_round_trips_exact_satoshi_values(satoshis: int) -> None:
    btc = Decimal(satoshis) / 100_000_000
    assert btc_to_satoshis(btc) == satoshis


@pytest.mark.parametrize(
    ("txid", "expected"),
    [
        ("A" * 64, "a" * 64),
        ("0123456789abcdef" * 4, "0123456789abcdef" * 4),
    ],
)
def test_txid_validation_and_canonicalization(txid: str, expected: str) -> None:
    assert ValidatedInputRecord.model_validate(make_source_record(txid=txid)).txid == expected


@pytest.mark.parametrize("txid", ["a" * 63, "a" * 65, "g" * 64])
def test_txid_validation_rejects_malformed_values(txid: str) -> None:
    assert _issue_for(txid=txid).code is ErrorCode.INVALID_TXID


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-28T12:30:00Z", datetime(2026, 8, 28, 12, 30, tzinfo=UTC)),
        ("2026-08-28T18:00:00+05:30", datetime(2026, 8, 28, 12, 30, tzinfo=UTC)),
        ("2026-08-28T08:30:00-04:00", datetime(2026, 8, 28, 12, 30, tzinfo=UTC)),
    ],
)
def test_timestamp_normalizes_to_utc(value: str, expected: datetime) -> None:
    record = ValidatedInputRecord.model_validate(make_source_record(timestamp=value))
    assert record.observed_at == expected
    assert record.observed_at.tzinfo is UTC


@pytest.mark.parametrize("timestamp", ["2026-08-28T12:30:00", "not-a-timestamp", 123])
def test_timestamp_rejects_ambiguous_or_malformed_values(timestamp: object) -> None:
    assert _issue_for(timestamp=timestamp).code is ErrorCode.INVALID_TIMESTAMP


@pytest.mark.parametrize(
    ("value", "expected"),
    [("192.0.2.1", "192.0.2.1"), ("2001:0db8::1", "2001:db8::1")],
)
def test_ip_validation_supports_ipv4_and_ipv6(value: str, expected: str) -> None:
    record = ValidatedInputRecord.model_validate(make_source_record(src_ip=value))
    assert record.src_ip == expected


def test_ip_validation_rejects_malformed_ip() -> None:
    assert _issue_for(src_ip="999.1.2.3").code is ErrorCode.INVALID_IP


@pytest.mark.parametrize("port", [0, 8333, 65_535, "8333"])
def test_port_validation_accepts_full_integer_range(port: int | str) -> None:
    assert ValidatedInputRecord.model_validate(make_source_record(src_port=port)).src_port == int(
        port
    )


@pytest.mark.parametrize("port", [-1, 65_536, "1.5", True])
def test_port_validation_rejects_out_of_range_or_non_integer(port: object) -> None:
    assert _issue_for(src_port=port).code is ErrorCode.INVALID_PORT


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        (
            {"input_addresses": ["A", "B"], "input_amounts": ["1"]},
            ErrorCode.INPUT_CARDINALITY_MISMATCH,
        ),
        (
            {"output_addresses": ["A", "B"], "output_amounts": ["1"]},
            ErrorCode.OUTPUT_CARDINALITY_MISMATCH,
        ),
        ({"input_addresses": "not-an-array"}, ErrorCode.MALFORMED_ARRAY),
        ({"output_addresses": ["  "]}, ErrorCode.INVALID_ADDRESS),
    ],
)
def test_array_and_address_validation(overrides: dict[str, Any], expected_code: ErrorCode) -> None:
    assert _issue_for(**overrides).code is expected_code


def test_deterministic_identifiers_are_stable_and_namespaced() -> None:
    source_id = build_source_record_id("f" * 64, 7)
    assert source_id == build_source_record_id("f" * 64, 7)
    assert source_id != build_source_record_id("f" * 64, 8)
    assert build_observation_id(source_id) == build_observation_id(source_id)
    assert build_observation_id(source_id) != source_id


def _issue_for(**overrides: Any) -> Any:
    with pytest.raises(ValidationError) as captured:
        ValidatedInputRecord.model_validate(make_source_record(**overrides))
    return validation_error_to_issue(captured.value)
