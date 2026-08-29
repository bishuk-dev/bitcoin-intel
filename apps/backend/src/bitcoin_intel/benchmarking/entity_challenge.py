from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bitcoin_intel.entity.evaluation import ENTITY_TRUTH_SCHEMA_VERSION
from bitcoin_intel.ingestion.validation import SATOSHIS_PER_BTC

ENTITY_CHALLENGE_PROFILE = "entity-challenge-v1"
_BASE_TIMESTAMP = datetime(2026, 2, 1, tzinfo=UTC)
_PARTITIONS = ("development", "validation", "test")


@dataclass(frozen=True, slots=True)
class EntityChallengeConfig:
    transaction_count: int
    seed: int = 42
    collaborative_interval: int = 20

    def __post_init__(self) -> None:
        if self.transaction_count < 30:
            raise ValueError("entity challenge requires at least 30 transactions")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if self.collaborative_interval < 5:
            raise ValueError("collaborative_interval must be at least 5")


@dataclass(frozen=True, slots=True)
class EntityChallengeSummary:
    output_path: Path
    profile: str
    transaction_count: int
    observation_count: int
    entity_count: int
    address_count: int
    collaborative_transaction_count: int
    source_sha256: str
    truth_sha256: str
    partition_entity_counts: dict[str, int]


def write_entity_challenge_bundle(
    output_path: Path, config: EntityChallengeConfig
) -> EntityChallengeSummary:
    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"entity challenge output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        source, truth = _build_bundle(config)
        (staging / "source.json").write_text(
            json.dumps(source, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8"
        )
        (staging / "entity-truth.json").write_text(
            json.dumps(truth, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        audit_entity_challenge_bundle(staging)
        if destination.exists():
            raise FileExistsError(
                f"entity challenge output was created concurrently: {destination}"
            )
        staging.replace(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    entities = truth["entities"]
    return EntityChallengeSummary(
        output_path=destination,
        profile=ENTITY_CHALLENGE_PROFILE,
        transaction_count=config.transaction_count,
        observation_count=len(source),
        entity_count=len(entities),
        address_count=sum(len(row["addresses"]) for row in entities),
        collaborative_transaction_count=len(truth["collaborative_transactions"]),
        source_sha256=_sha256_file(destination / "source.json"),
        truth_sha256=_sha256_file(destination / "entity-truth.json"),
        partition_entity_counts=dict(
            sorted(Counter(str(row["partition"]) for row in entities).items())
        ),
    )


def audit_entity_challenge_bundle(bundle_path: Path) -> dict[str, Any]:
    root = bundle_path.expanduser().resolve(strict=True)
    source = json.loads((root / "source.json").read_text(encoding="utf-8"))
    truth = json.loads((root / "entity-truth.json").read_text(encoding="utf-8"))
    if not isinstance(source, list) or not isinstance(truth, dict):
        raise ValueError("entity challenge documents are malformed")
    if truth.get("truth_schema_version") != ENTITY_TRUTH_SCHEMA_VERSION:
        raise ValueError("entity challenge truth schema is unsupported")
    forbidden = {
        "entity_id",
        "partition",
        "entity_kind",
        "collaborative",
        "participant_entity_ids",
        "evaluation_only",
    }
    source_addresses: set[str] = set()
    definitions: dict[str, str] = {}
    for record in source:
        if not isinstance(record, dict) or forbidden.intersection(record):
            raise ValueError("entity truth leaked into source records")
        txid = str(record["txid"])
        definition = json.dumps(
            {
                key: record[key]
                for key in (
                    "input_addresses",
                    "output_addresses",
                    "input_amounts",
                    "output_amounts",
                    "fee",
                    "script_type",
                )
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        if txid in definitions and definitions[txid] != definition:
            raise ValueError("duplicate observations conflict on transaction definition")
        definitions[txid] = definition
        source_addresses.update(map(str, record["input_addresses"]))
        source_addresses.update(map(str, record["output_addresses"]))
    truth_addresses: list[str] = []
    entity_ids: set[str] = set()
    for row in truth.get("entities", []):
        if not isinstance(row, dict) or row.get("partition") not in _PARTITIONS:
            raise ValueError("entity truth row is malformed")
        entity_id = str(row["entity_id"])
        if entity_id in entity_ids:
            raise ValueError("entity truth ID is duplicated")
        entity_ids.add(entity_id)
        truth_addresses.extend(map(str, row["addresses"]))
    if len(truth_addresses) != len(set(truth_addresses)):
        raise ValueError("an address belongs to multiple truth entities")
    if set(truth_addresses) != source_addresses:
        raise ValueError("truth and source address coverage differ")
    return {
        "truth_columns_in_source": 0,
        "duplicate_truth_addresses": 0,
        "conflicting_transaction_definitions": 0,
        "entity_safe_partitions": True,
        "source_address_coverage": len(source_addresses),
        "status": "passed",
    }


def _build_bundle(config: EntityChallengeConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entity_count = max(9, math.ceil(config.transaction_count / 8))
    entities: list[dict[str, Any]] = []
    partition_indices: dict[str, list[int]] = {name: [] for name in _PARTITIONS}
    for index in range(entity_count):
        partition = _partition(index)
        partition_indices[partition].append(index)
        kind = "singleton" if index % 10 == 0 else "hub" if index % 17 == 0 else "standard"
        member_count = 1 if kind == "singleton" else 6 if kind == "hub" else 2 + (index % 3)
        entities.append(
            {
                "entity_id": _identifier(config.seed, "truth-entity", index, prefix="et-"),
                "addresses": [
                    _identifier(config.seed, f"address-{index}", member, prefix="bc1q")[:42]
                    for member in range(member_count)
                ],
                "partition": partition,
                "entity_kind": kind,
            }
        )
    records_by_tx: list[list[dict[str, Any]]] = []
    collaborative_truth: list[dict[str, Any]] = []
    for tx_index in range(config.transaction_count):
        # Offset each collaborative interval so multi-party examples rotate through all
        # entity-safe partitions instead of aliasing with the modulo-10 partition rule.
        source_index = (tx_index + tx_index // config.collaborative_interval) % entity_count
        source_entity = entities[source_index]
        partition = str(source_entity["partition"])
        cycle = tx_index // entity_count
        is_collaborative = tx_index % config.collaborative_interval == 0
        if is_collaborative:
            candidates = partition_indices[partition]
            participant_count = 3 if (tx_index // config.collaborative_interval) % 7 == 0 else 4
            participant_indices = [
                candidates[(candidates.index(source_index) + offset) % len(candidates)]
                for offset in range(min(participant_count, len(candidates)))
            ]
            input_addresses = [
                str(
                    entities[index]["addresses"][
                        (cycle + index) % len(entities[index]["addresses"])
                    ]
                )
                for index in participant_indices
            ]
            output_entities = [
                candidates[
                    (candidates.index(source_index) + participant_count + offset) % len(candidates)
                ]
                for offset in range(max(4, participant_count))
            ]
            output_addresses = [
                str(entities[index]["addresses"][(cycle + 1) % len(entities[index]["addresses"])])
                for index in output_entities
            ]
            input_sats = [
                150_000_000 + (position * 10_000_000) for position in range(len(input_addresses))
            ]
            fee_sats = 2_000
            equal_value = (sum(input_sats) - fee_sats) // len(output_addresses)
            output_sats = [equal_value] * len(output_addresses)
            output_sats[-1] += sum(input_sats) - fee_sats - sum(output_sats)
            collaborative_truth.append(
                {
                    "txid": _txid(config.seed, tx_index),
                    "partition": partition,
                    "input_addresses": input_addresses,
                    "participant_entity_ids": [
                        entities[index]["entity_id"] for index in participant_indices
                    ],
                    "variant": "weak-three-party" if participant_count == 3 else "equal-output",
                }
            )
        else:
            addresses = list(map(str, source_entity["addresses"]))
            legitimate_collaborative_shape = len(addresses) >= 4 and tx_index % 37 == 0
            input_count = (
                4 if legitimate_collaborative_shape else min(len(addresses), 2 + (cycle % 2))
            )
            input_addresses = [
                addresses[(cycle + offset) % len(addresses)] for offset in range(input_count)
            ]
            destination_index = (source_index + 1 + cycle) % entity_count
            destination = entities[destination_index]
            output_count = 4 if legitimate_collaborative_shape else 2 + (tx_index % 2)
            output_addresses = [
                str(destination["addresses"][(cycle + offset) % len(destination["addresses"])])
                for offset in range(output_count - 1)
            ]
            output_addresses.append(addresses[(cycle + input_count) % len(addresses)])
            input_sats = [100_000_000 + (position * 5_000_000) for position in range(input_count)]
            fee_sats = 1_000 + (tx_index % 500)
            spendable = sum(input_sats) - fee_sats
            if legitimate_collaborative_shape:
                base, remainder = divmod(spendable, output_count)
                output_sats = [base] * output_count
                output_sats[-1] += remainder
            else:
                base, remainder = divmod(spendable, output_count)
                output_sats = [base + position * 137 for position in range(output_count)]
                output_sats[-1] += remainder - 137 * sum(range(output_count))
        txid = _txid(config.seed, tx_index)
        observation_count = 2 if tx_index % 7 == 0 else 1
        observations: list[dict[str, Any]] = []
        for observation_index in range(observation_count):
            shared_infrastructure = (source_index + observation_index) % 8
            observed_at = _BASE_TIMESTAMP + timedelta(seconds=tx_index * 41 + observation_index)
            observations.append(
                {
                    "timestamp": observed_at.isoformat().replace("+00:00", "Z"),
                    "src_ip": f"203.0.113.{1 + shared_infrastructure}",
                    "dst_ip": f"198.51.100.{1 + ((tx_index * 13 + observation_index) % 200)}",
                    "src_port": 8_333 if observation_index == 0 else 18_333,
                    "dst_port": 10_000 + ((tx_index + observation_index) % 50_000),
                    "txid": txid,
                    "input_addresses": input_addresses,
                    "output_addresses": output_addresses,
                    "input_amounts": [_sats_to_btc(value) for value in input_sats],
                    "output_amounts": [_sats_to_btc(value) for value in output_sats],
                    "fee": _sats_to_btc(fee_sats),
                    "script_type": ("p2wpkh", "p2tr", "p2pkh")[tx_index % 3],
                    "geo_country": ("IN", "US", "DE", "SG")[shared_infrastructure % 4],
                    "asn": 64_512 + shared_infrastructure,
                }
            )
        records_by_tx.append(observations)
    multiplier, offset = _permutation(config.transaction_count, config.seed)
    source = [
        record
        for position in range(config.transaction_count)
        for record in records_by_tx[(multiplier * position + offset) % config.transaction_count]
    ]
    truth = {
        "truth_schema_version": ENTITY_TRUTH_SCHEMA_VERSION,
        "evaluation_only": True,
        "not_criminal_ground_truth": True,
        "purpose": "ownership-heuristic and community evaluation only",
        "configuration": {
            "profile": ENTITY_CHALLENGE_PROFILE,
            "seed": config.seed,
            "transaction_count": config.transaction_count,
            "collaborative_interval": config.collaborative_interval,
            "entity_safe_partition_policy": (
                "entity generation index modulo deterministic 60/20/20 split"
            ),
        },
        "entities": entities,
        "collaborative_transactions": sorted(collaborative_truth, key=lambda row: row["txid"]),
        "network_trap": (
            "Source IPs are intentionally reused across truth entities; "
            "they are not ownership truth."
        ),
    }
    return source, truth


def _partition(index: int) -> str:
    bucket = index % 10
    return "development" if bucket < 6 else "validation" if bucket < 8 else "test"


def _permutation(size: int, seed: int) -> tuple[int, int]:
    candidate = 1 + (seed % max(1, size - 1))
    while math.gcd(candidate, size) != 1:
        candidate += 1
    return candidate, seed % size


def _identifier(seed: int, namespace: str, index: int, prefix: str) -> str:
    return (
        prefix
        + hashlib.sha256(
            f"{ENTITY_CHALLENGE_PROFILE}:{seed}:{namespace}:{index}".encode()
        ).hexdigest()
    )


def _txid(seed: int, index: int) -> str:
    return hashlib.sha256(f"{ENTITY_CHALLENGE_PROFILE}:{seed}:tx:{index}".encode()).hexdigest()


def _sats_to_btc(value: int) -> str:
    whole, fraction = divmod(value, SATOSHIS_PER_BTC)
    return str(whole) if not fraction else f"{whole}.{fraction:08d}".rstrip("0")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
