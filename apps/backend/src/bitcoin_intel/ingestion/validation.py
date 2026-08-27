from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation

from bitcoin_intel.ingestion.errors import ErrorCode, MoneyConversionError

SATOSHIS_PER_BTC = 100_000_000
MAX_BITCOIN_SATS = 21_000_000 * SATOSHIS_PER_BTC


def btc_to_satoshis(value: object) -> int:
    """Convert a decimal BTC value to exact integer satoshis without rounding."""

    if isinstance(value, (bool, float)):
        raise MoneyConversionError(
            ErrorCode.INVALID_AMOUNT,
            "amount must be supplied as a decimal string or exact decimal/integer value",
        )
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MoneyConversionError(
            ErrorCode.INVALID_AMOUNT, "amount is not a valid decimal BTC value"
        ) from error

    if not amount.is_finite():
        raise MoneyConversionError(ErrorCode.INVALID_AMOUNT, "amount must be finite")
    if amount < 0:
        raise MoneyConversionError(ErrorCode.NEGATIVE_AMOUNT, "amount must not be negative")

    scaled = amount * SATOSHIS_PER_BTC
    if scaled != scaled.to_integral_value():
        raise MoneyConversionError(
            ErrorCode.AMOUNT_PRECISION_EXCEEDED,
            "amount cannot be represented exactly in satoshis",
        )
    satoshis = int(scaled)
    if satoshis > MAX_BITCOIN_SATS:
        raise MoneyConversionError(
            ErrorCode.INVALID_AMOUNT, "amount exceeds the maximum Bitcoin supply"
        )
    return satoshis


def build_source_record_id(source_file_sha256: str, record_index: int) -> str:
    if record_index < 0:
        raise ValueError("record_index must be non-negative")
    return _sha256_text(f"source-record:{source_file_sha256}:{record_index}")


def build_observation_id(source_record_id: str) -> str:
    return _sha256_text(f"network-observation:{source_record_id}")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
