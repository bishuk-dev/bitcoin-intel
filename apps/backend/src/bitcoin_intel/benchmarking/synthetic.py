from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bitcoin_intel.ingestion.validation import SATOSHIS_PER_BTC

_BASE_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
_COUNTRIES = ("IN", "US", "DE", "SG", "JP", "GB")
_SCRIPT_TYPES = ("p2wpkh", "p2tr", "p2pkh")


@dataclass(frozen=True, slots=True)
class SyntheticConfig:
    record_count: int
    seed: int = 42
    duplicate_observation_ratio: float = 0.2
    min_inputs: int = 1
    max_inputs: int = 3
    min_outputs: int = 1
    max_outputs: int = 3
    ipv6_ratio: float = 0.25

    def __post_init__(self) -> None:
        if self.record_count <= 0:
            raise ValueError("record_count must be positive")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if not 0 <= self.duplicate_observation_ratio < 1:
            raise ValueError("duplicate_observation_ratio must be in [0, 1)")
        if not 1 <= self.min_inputs <= self.max_inputs <= 8:
            raise ValueError("input count range must satisfy 1 <= min <= max <= 8")
        if not 1 <= self.min_outputs <= self.max_outputs <= 8:
            raise ValueError("output count range must satisfy 1 <= min <= max <= 8")
        if not 0 <= self.ipv6_ratio <= 1:
            raise ValueError("ipv6_ratio must be in [0, 1]")

    @property
    def unique_transaction_count(self) -> int:
        return max(1, int(self.record_count * (1 - self.duplicate_observation_ratio)))


@dataclass(frozen=True, slots=True)
class SyntheticGenerationSummary:
    record_count: int
    unique_transaction_count: int
    source_bytes: int
    sample_txid: str
    sample_address: str
    sample_ip: str


def write_synthetic_json(path: Path, config: SyntheticConfig) -> SyntheticGenerationSummary:
    """Stream a deterministic, Phase 1-valid benchmark-only JSON array."""

    path.parent.mkdir(parents=True, exist_ok=True)
    unique_count = config.unique_transaction_count
    sample_transaction_index = unique_count // 2
    sample_record_index = sample_transaction_index

    with path.open("w", encoding="utf-8", newline="\n") as source:
        source.write("[\n")
        for record_index in range(config.record_count):
            if record_index:
                source.write(",\n")
            transaction_index = (
                record_index
                if record_index < unique_count
                else (record_index - unique_count) % unique_count
            )
            record = _build_record(config, transaction_index, record_index)
            json.dump(record, source, separators=(",", ":"), sort_keys=True)
        source.write("\n]\n")

    return SyntheticGenerationSummary(
        record_count=config.record_count,
        unique_transaction_count=unique_count,
        source_bytes=path.stat().st_size,
        sample_txid=synthetic_txid(config.seed, sample_transaction_index),
        sample_address=synthetic_input_address(config.seed, sample_transaction_index, 0),
        sample_ip=_network_values(config, sample_record_index)[0],
    )


def synthetic_txid(seed: int, transaction_index: int) -> str:
    return _digest(f"phase-2-benchmark:{seed}:tx:{transaction_index}")


def synthetic_input_address(seed: int, transaction_index: int, input_index: int) -> str:
    prefix = synthetic_txid(seed, transaction_index)[:20]
    return f"bench-in-{prefix}-{input_index}"


def _build_record(
    config: SyntheticConfig, transaction_index: int, record_index: int
) -> dict[str, Any]:
    transaction = _transaction_values(config, transaction_index)
    src_ip, dst_ip = _network_values(config, record_index)
    observed_at = _BASE_TIMESTAMP + timedelta(seconds=(record_index * 37) % (30 * 24 * 60 * 60))
    has_reported_metadata = record_index % 11 != 0
    return {
        "timestamp": observed_at.isoformat().replace("+00:00", "Z"),
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": 18_333 if record_index % 3 == 0 else 8_333,
        "dst_port": 1_024 + (record_index % 64_512),
        "txid": synthetic_txid(config.seed, transaction_index),
        **transaction,
        "geo_country": _COUNTRIES[record_index % len(_COUNTRIES)]
        if has_reported_metadata
        else None,
        "asn": 64_512 + (record_index % 16) if has_reported_metadata else None,
    }


def _transaction_values(config: SyntheticConfig, transaction_index: int) -> dict[str, Any]:
    generator = random.Random(_seed_integer(config.seed, "transaction", transaction_index))
    input_count = generator.randint(config.min_inputs, config.max_inputs)
    output_count = generator.randint(config.min_outputs, config.max_outputs)
    input_sats = [50_000_000 + generator.randrange(50_000_000) for _ in range(input_count)]
    fee_sats = 500 + generator.randrange(5_000)
    spendable_sats = sum(input_sats) - fee_sats
    output_base, output_remainder = divmod(spendable_sats, output_count)
    output_sats = [
        output_base + (1 if output_index < output_remainder else 0)
        for output_index in range(output_count)
    ]
    txid_prefix = synthetic_txid(config.seed, transaction_index)[:20]
    return {
        "input_addresses": [
            synthetic_input_address(config.seed, transaction_index, input_index)
            for input_index in range(input_count)
        ],
        "output_addresses": [
            f"bench-out-{txid_prefix}-{output_index}" for output_index in range(output_count)
        ],
        "input_amounts": [_sats_to_btc(value) for value in input_sats],
        "output_amounts": [_sats_to_btc(value) for value in output_sats],
        "fee": _sats_to_btc(fee_sats),
        "script_type": _SCRIPT_TYPES[transaction_index % len(_SCRIPT_TYPES)],
    }


def _network_values(config: SyntheticConfig, record_index: int) -> tuple[str, str]:
    generator = random.Random(_seed_integer(config.seed, "observation", record_index))
    if generator.random() < config.ipv6_ratio:
        source_suffix = 1 + (record_index % 65_534)
        destination_suffix = 1 + ((record_index * 17) % 65_534)
        return f"2001:db8::{source_suffix:x}", f"2001:db8:1::{destination_suffix:x}"
    return (
        f"192.0.2.{1 + (record_index % 254)}",
        f"198.51.100.{1 + ((record_index * 17) % 254)}",
    )


def _sats_to_btc(satoshis: int) -> str:
    whole, fraction = divmod(satoshis, SATOSHIS_PER_BTC)
    if not fraction:
        return str(whole)
    return f"{whole}.{fraction:08d}".rstrip("0")


def _seed_integer(seed: int, category: str, index: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}:{category}:{index}".encode()).digest()[:8], "big")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
