from __future__ import annotations

import hashlib
import json
import random
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bitcoin_intel.ingestion.validation import SATOSHIS_PER_BTC

SCENARIO_NAMES = (
    "baseline",
    "high_fan_out_pattern",
    "rapid_sequence_pattern",
    "shared_network_pattern",
    "high_value_pattern",
)
DEFAULT_SCENARIO_PROPORTIONS = (
    ("high_fan_out_pattern", 0.15),
    ("rapid_sequence_pattern", 0.15),
    ("shared_network_pattern", 0.15),
    ("high_value_pattern", 0.15),
)
_BASE_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    transaction_count: int
    seed: int = 42
    scenario_proportions: tuple[tuple[str, float], ...] = DEFAULT_SCENARIO_PROPORTIONS

    def __post_init__(self) -> None:
        if self.transaction_count <= 0:
            raise ValueError("transaction_count must be positive")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        names = [name for name, _ in self.scenario_proportions]
        if len(names) != len(set(names)):
            raise ValueError("scenario proportions contain duplicate names")
        if any(name not in SCENARIO_NAMES or name == "baseline" for name in names):
            raise ValueError("only supported non-baseline scenario names may be configured")
        if any(proportion < 0 or proportion > 1 for _, proportion in self.scenario_proportions):
            raise ValueError("scenario proportions must be in [0, 1]")
        if sum(proportion for _, proportion in self.scenario_proportions) > 1:
            raise ValueError("scenario proportions must sum to at most 1")


@dataclass(frozen=True, slots=True)
class ScenarioGenerationSummary:
    output_path: Path
    transaction_count: int
    observation_count: int
    source_sha256: str
    truth_sha256: str
    scenario_counts: dict[str, int]


def write_scenario_bundle(output_path: Path, config: ScenarioConfig) -> ScenarioGenerationSummary:
    """Atomically publish canonical-compatible source data and isolated evaluation truth."""

    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"scenario output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        counts = {name: 0 for name in SCENARIO_NAMES}
        source_path = staging / "source.json"
        truth_path = staging / "scenario-truth.json"
        observation_count = _write_bundle_files(source_path, truth_path, config, counts)
        if destination.exists():
            raise FileExistsError(f"scenario output was created concurrently: {destination}")
        staging.replace(destination)
        return ScenarioGenerationSummary(
            output_path=destination,
            transaction_count=config.transaction_count,
            observation_count=observation_count,
            source_sha256=_sha256_file(destination / "source.json"),
            truth_sha256=_sha256_file(destination / "scenario-truth.json"),
            scenario_counts={name: count for name, count in counts.items() if count},
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _scenario_for(config: ScenarioConfig, transaction_index: int) -> str:
    value = random.Random(_seed(config.seed, "scenario", transaction_index)).random()
    cumulative = 0.0
    for name, proportion in sorted(config.scenario_proportions):
        cumulative += proportion
        if value < cumulative:
            return name
    return "baseline"


def _build_transaction(
    config: ScenarioConfig, transaction_index: int, scenario: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    generator = random.Random(_seed(config.seed, "transaction", transaction_index))
    txid = hashlib.sha256(
        f"phase-4-scenario:{config.seed}:tx:{transaction_index}".encode()
    ).hexdigest()
    chain_input = f"scenario-chain-{config.seed}-{transaction_index}"
    chain_output = f"scenario-chain-{config.seed}-{transaction_index + 1}"
    input_addresses = [chain_input]
    input_sats = [250_000_000 + generator.randrange(50_000_000)]

    if transaction_index % 3 == 0:
        input_addresses.append(f"scenario-reused-input-{config.seed}-{transaction_index % 11}")
        input_sats.append(100_000_000 + generator.randrange(25_000_000))
    if transaction_index % 7 == 0:
        input_addresses.append(f"scenario-hub-{config.seed}")
        input_sats.append(75_000_000)

    output_count = 6 if scenario == "high_fan_out_pattern" else 2 + transaction_index % 2
    output_addresses = [chain_output] + [
        f"scenario-output-{config.seed}-{transaction_index}-{index}"
        for index in range(1, output_count)
    ]
    if transaction_index % 13 == 0:
        output_addresses[-1] = input_addresses[0]

    if scenario == "high_value_pattern":
        input_sats = [value * 100 for value in input_sats]
    fee_sats = 1_000 + transaction_index % 10_000
    spendable = sum(input_sats) - fee_sats
    output_base, remainder = divmod(spendable, output_count)
    output_sats = [output_base + (1 if index < remainder else 0) for index in range(output_count)]
    transaction = {
        "txid": txid,
        "input_addresses": input_addresses,
        "output_addresses": output_addresses,
        "input_amounts": [_sats_to_btc(value) for value in input_sats],
        "output_amounts": [_sats_to_btc(value) for value in output_sats],
        "fee": _sats_to_btc(fee_sats),
        "script_type": ("p2wpkh", "p2tr", "p2pkh")[transaction_index % 3],
    }

    base_time = _BASE_TIMESTAMP + timedelta(minutes=transaction_index * 15)
    if scenario == "rapid_sequence_pattern":
        base_time = _BASE_TIMESTAMP + timedelta(
            hours=transaction_index // 20, seconds=transaction_index % 20
        )
    observation_count = 3 if scenario in {"rapid_sequence_pattern", "shared_network_pattern"} else 1
    if transaction_index % 5 == 0:
        observation_count += 1
    records: list[dict[str, Any]] = []
    observed_ips: list[tuple[str, str]] = []
    for observation_index in range(observation_count):
        if scenario == "shared_network_pattern":
            src_ip = f"198.51.100.{1 + transaction_index % 4}"
        else:
            src_ip = f"192.0.2.{1 + (transaction_index + observation_index * 17) % 254}"
        dst_ip = (
            f"2001:db8::{1 + (transaction_index * 7 + observation_index) % 65_534:x}"
            if observation_index % 2
            else f"203.0.113.{1 + (transaction_index * 5 + observation_index) % 254}"
        )
        offset = observation_index * (5 if scenario == "rapid_sequence_pattern" else 300)
        if observation_index == observation_count - 1 and transaction_index % 5 == 0:
            offset += 7 * 24 * 60 * 60
        record = {
            "timestamp": (base_time + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
            "src_ip": src_ip,
            "dst_ip": dst_ip,
            "src_port": 8_333 if observation_index % 2 == 0 else 18_333,
            "dst_port": 10_000 + (transaction_index + observation_index) % 50_000,
            **transaction,
            "geo_country": ("IN", "US", "DE", "SG")[transaction_index % 4],
            "asn": 64_512 + transaction_index % 8,
        }
        records.append(record)
        observed_ips.append((src_ip, dst_ip))

    truth = {
        "txid": txid,
        "scenario_class": scenario,
        "structural_truth": {
            "input_count": len(input_addresses),
            "output_count": len(output_addresses),
            "observation_count": observation_count,
            "has_self_address_appearance": bool(set(input_addresses) & set(output_addresses)),
            "chain_input": chain_input,
            "chain_output": chain_output,
            "observed_endpoints": observed_ips,
        },
    }
    return records, truth


def _write_bundle_files(
    source_path: Path,
    truth_path: Path,
    config: ScenarioConfig,
    counts: dict[str, int],
) -> int:
    truth_prefix = {
        "configuration": {
            "scenario_proportions": dict(sorted(config.scenario_proportions)),
            "seed": config.seed,
            "transaction_count": config.transaction_count,
        },
        "not_criminal_ground_truth": True,
        "purpose": "evaluation-only deterministic structural scenario truth",
        "truth_schema_version": "1.0.0",
    }
    observation_count = 0
    with (
        source_path.open("w", encoding="utf-8", newline="\n") as source,
        truth_path.open("w", encoding="utf-8", newline="\n") as truth,
    ):
        source.write("[")
        truth.write(json.dumps(truth_prefix, separators=(",", ":"), sort_keys=True)[:-1])
        truth.write(',"transactions":[')
        first_record = True
        for transaction_index in range(config.transaction_count):
            scenario = _scenario_for(config, transaction_index)
            counts[scenario] += 1
            records, truth_row = _build_transaction(config, transaction_index, scenario)
            if transaction_index:
                truth.write(",")
            json.dump(truth_row, truth, separators=(",", ":"), sort_keys=True)
            for record in records:
                if not first_record:
                    source.write(",")
                json.dump(record, source, separators=(",", ":"), sort_keys=True)
                first_record = False
                observation_count += 1
        source.write("]\n")
        truth.write("]}\n")
    return observation_count


def _sats_to_btc(satoshis: int) -> str:
    whole, fraction = divmod(satoshis, SATOSHIS_PER_BTC)
    return str(whole) if not fraction else f"{whole}.{fraction:08d}".rstrip("0")


def _seed(seed: int, category: str, index: int) -> int:
    payload = f"{seed}:{category}:{index}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
