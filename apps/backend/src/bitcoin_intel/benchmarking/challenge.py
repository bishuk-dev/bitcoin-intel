from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bitcoin_intel.benchmarking.scenarios import SCENARIO_NAMES
from bitcoin_intel.ingestion.validation import SATOSHIS_PER_BTC

CHALLENGE_PROFILE = "challenge-v1"
CHALLENGE_TRUTH_SCHEMA_VERSION = "1.2.0"
INTENSITIES = ("weak", "medium", "strong")
_BASE_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
_COMMON_PORTS = (8333, 18333, 38333, 18444, 9333, 443)
_SCRIPT_TYPES = ("p2wpkh", "p2tr", "p2pkh", "p2sh")
_SCENARIO_WEIGHTS = (
    ("baseline", 0.40),
    ("high_fan_out_pattern", 0.15),
    ("rapid_sequence_pattern", 0.15),
    ("shared_network_pattern", 0.15),
    ("high_value_pattern", 0.15),
)


@dataclass(frozen=True, slots=True)
class ChallengeConfig:
    transaction_count: int
    seed: int = 42
    group_size: int = 16

    def __post_init__(self) -> None:
        if self.transaction_count <= 0:
            raise ValueError("transaction_count must be positive")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if self.group_size < 4:
            raise ValueError("group_size must be at least 4")


@dataclass(frozen=True, slots=True)
class ChallengeGenerationSummary:
    output_path: Path
    profile: str
    transaction_count: int
    observation_count: int
    group_count: int
    source_sha256: str
    truth_sha256: str
    scenario_counts: dict[str, int]
    intensity_counts: dict[str, int]
    fingerprint_audit: dict[str, Any]


def write_challenge_bundle(
    output_path: Path, config: ChallengeConfig
) -> ChallengeGenerationSummary:
    """Publish harder synthetic records and evaluation-only truth atomically."""

    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"challenge output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        source_path = staging / "source.json"
        truth_path = staging / "scenario-truth.json"
        observation_count, scenario_counts, intensity_counts = _write_files(
            source_path, truth_path, config
        )
        fingerprint_audit = audit_challenge_bundle(staging)
        if destination.exists():
            raise FileExistsError(f"challenge output was created concurrently: {destination}")
        staging.replace(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return ChallengeGenerationSummary(
        output_path=destination,
        profile=CHALLENGE_PROFILE,
        transaction_count=config.transaction_count,
        observation_count=observation_count,
        group_count=math.ceil(config.transaction_count / config.group_size),
        source_sha256=_sha256_file(destination / "source.json"),
        truth_sha256=_sha256_file(destination / "scenario-truth.json"),
        scenario_counts=dict(sorted(scenario_counts.items())),
        intensity_counts=dict(sorted(intensity_counts.items())),
        fingerprint_audit=fingerprint_audit,
    )


def audit_challenge_bundle(bundle_path: Path) -> dict[str, Any]:
    """Reject obvious generator fingerprints while allowing intended behavioural signals."""

    root = bundle_path.expanduser().resolve(strict=True)
    source = json.loads((root / "source.json").read_text(encoding="utf-8"))
    truth = json.loads((root / "scenario-truth.json").read_text(encoding="utf-8"))
    if not isinstance(source, list) or not isinstance(truth, dict):
        raise ValueError("challenge bundle documents are malformed")
    truth_rows = truth.get("transactions")
    if not isinstance(truth_rows, list):
        raise ValueError("challenge truth transactions are malformed")
    labels = {str(row["txid"]): str(row["scenario_class"]) for row in truth_rows}
    groups = {str(row["txid"]): str(row["scenario_group_id"]) for row in truth_rows}
    record_labels: defaultdict[str, set[str]] = defaultdict(set)
    identifier_groups: defaultdict[str, set[str]] = defaultdict(set)
    ordered_labels: list[str] = []
    last_txid: str | None = None
    forbidden_keys = {
        "scenario_class",
        "scenario_group_id",
        "scenario_intensity",
        "secondary_tags",
        "structural_truth",
    }
    for raw in source:
        if not isinstance(raw, dict) or forbidden_keys.intersection(raw):
            raise ValueError("challenge truth leaked into canonical source records")
        txid = str(raw["txid"])
        if txid not in labels:
            raise ValueError("challenge source contains a transaction without truth metadata")
        label = labels[txid]
        if txid != last_txid:
            ordered_labels.append(label)
            last_txid = txid
        group = groups[txid]
        for field in ("src_port", "script_type"):
            record_labels[f"{field}:{raw[field]}"].add(label)
        for ip_field in ("src_ip", "dst_ip"):
            ip = str(raw[ip_field])
            identifier_groups[f"ip:{ip}"].add(group)
            prefix = ":".join(ip.split(":")[:3])
            record_labels[f"ip_prefix:{prefix}"].add(label)
        for address in [*raw["input_addresses"], *raw["output_addresses"]]:
            identifier_groups[f"address:{address}"].add(group)
        serialized_identity = " ".join(
            [txid, *map(str, raw["input_addresses"]), *map(str, raw["output_addresses"])]
        ).lower()
        if any(name in serialized_identity for name in SCENARIO_NAMES):
            raise ValueError("scenario name leaked into a generated identity")

    cross_group = [name for name, owners in identifier_groups.items() if len(owners) > 1]
    if cross_group:
        raise ValueError(f"related challenge identities cross groups: {cross_group[0]}")
    single_class_categories = {
        name: sorted(values) for name, values in record_labels.items() if len(values) < 2
    }
    if single_class_categories:
        first = next(iter(single_class_categories))
        raise ValueError(f"categorical generator fingerprint detected: {first}")
    longest_run = _longest_run(ordered_labels)
    if len(ordered_labels) >= 100 and longest_run > 12:
        raise ValueError("scenario ordering contains an implausibly long class run")
    return {
        "truth_columns_in_source": 0,
        "scenario_names_in_identifiers": 0,
        "cross_group_identifier_count": 0,
        "single_class_port_script_or_ip_prefix_count": 0,
        "longest_ordered_class_run": longest_run,
        "audited_transactions": len(ordered_labels),
        "audited_observations": len(source),
        "status": "passed",
    }


def _write_files(
    source_path: Path, truth_path: Path, config: ChallengeConfig
) -> tuple[int, Counter[str], Counter[str]]:
    scenario_counts: Counter[str] = Counter()
    intensity_counts: Counter[str] = Counter()
    truth_prefix = {
        "configuration": {
            "group_size": config.group_size,
            "profile": CHALLENGE_PROFILE,
            "scenario_weights": dict(_SCENARIO_WEIGHTS),
            "seed": config.seed,
            "transaction_count": config.transaction_count,
        },
        "not_criminal_ground_truth": True,
        "purpose": "evaluation-only harder overlapping structural scenario truth",
        "truth_schema_version": CHALLENGE_TRUTH_SCHEMA_VERSION,
    }
    multiplier, offset = _permutation(config.transaction_count, config.seed)
    observation_count = 0
    with (
        source_path.open("w", encoding="utf-8", newline="\n") as source,
        truth_path.open("w", encoding="utf-8", newline="\n") as truth,
    ):
        source.write("[")
        truth.write(json.dumps(truth_prefix, separators=(",", ":"), sort_keys=True)[:-1])
        truth.write(',"transactions":[')
        first_record = True
        for output_position in range(config.transaction_count):
            transaction_index = (multiplier * output_position + offset) % config.transaction_count
            scenario, intensity, secondary_tags = _truth_for(config, transaction_index)
            scenario_counts[scenario] += 1
            intensity_counts[intensity] += 1
            records, truth_row = _build_transaction(
                config, transaction_index, scenario, intensity, secondary_tags
            )
            if output_position:
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
    return observation_count, scenario_counts, intensity_counts


def _truth_for(config: ChallengeConfig, transaction_index: int) -> tuple[str, str, list[str]]:
    generator = random.Random(_seed(config.seed, "truth", transaction_index))
    draw = generator.random()
    cumulative = 0.0
    scenario = "baseline"
    for name, weight in _SCENARIO_WEIGHTS:
        cumulative += weight
        if draw < cumulative:
            scenario = name
            break
    intensity = "not_applicable"
    if scenario != "baseline":
        intensity_draw = generator.random()
        intensity = (
            "weak" if intensity_draw < 0.40 else "medium" if intensity_draw < 0.80 else "strong"
        )
    secondary_tags: list[str] = []
    if generator.random() < 0.22:
        candidates = [name for name in SCENARIO_NAMES if name not in {"baseline", scenario}]
        secondary_tags.append(candidates[generator.randrange(len(candidates))])
    return scenario, intensity, secondary_tags


def _build_transaction(
    config: ChallengeConfig,
    transaction_index: int,
    scenario: str,
    intensity: str,
    secondary_tags: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    generator = random.Random(_seed(config.seed, "record", transaction_index))
    group_index, group_position = divmod(transaction_index, config.group_size)
    group_token = hashlib.sha256(
        f"challenge-group:{config.seed}:{group_index}".encode()
    ).hexdigest()[:16]
    group_id = f"cg-{group_token}"
    txid = hashlib.sha256(
        f"{CHALLENGE_PROFILE}:{config.seed}:tx:{transaction_index}".encode()
    ).hexdigest()

    input_count = 1 + min(4, int(generator.expovariate(1.1)))
    output_count = 1 + min(8, int(generator.expovariate(0.65)))
    if generator.random() < 0.08:
        output_count += generator.randint(2, 6)
    active_tags = {scenario, *secondary_tags}
    intensity_shift = {"not_applicable": 0, "weak": 1, "medium": 3, "strong": 6}[intensity]
    if "high_fan_out_pattern" in active_tags:
        output_count += intensity_shift
    output_count = min(output_count, 15)

    input_addresses = [
        f"bc1q{group_token}i{(group_position + index) % (config.group_size + 5):04x}"
        for index in range(input_count)
    ]
    output_addresses = [
        f"bc1q{group_token}o{(group_position * 3 + index) % (config.group_size * 4):04x}"
        for index in range(output_count)
    ]
    if generator.random() < 0.10:
        output_addresses[-1] = input_addresses[0]

    base_sats = max(50_000, int(generator.lognormvariate(18.2, 1.45)))
    if generator.random() < 0.07:
        base_sats *= generator.randint(4, 16)
    if "high_value_pattern" in active_tags:
        base_sats = int(base_sats * {"weak": 1.6, "medium": 3.2, "strong": 7.0}.get(intensity, 1.4))
    input_sats = [
        max(10_000, int(base_sats * generator.uniform(0.45, 1.55))) for _ in range(input_count)
    ]
    fee_sats = min(sum(input_sats) - output_count, generator.randint(500, 25_000))
    spendable = sum(input_sats) - fee_sats
    weights = [generator.uniform(0.5, 1.5) for _ in range(output_count)]
    output_sats = _partition(spendable, weights)

    observation_count = generator.randint(1, 5)
    if generator.random() < 0.12:
        observation_count += generator.randint(2, 5)
    if "rapid_sequence_pattern" in active_tags:
        observation_count += {"weak": 1, "medium": 2, "strong": 4}.get(intensity, 1)
    if "shared_network_pattern" in active_tags:
        observation_count += {"weak": 0, "medium": 1, "strong": 2}.get(intensity, 1)
    observation_count = min(observation_count, 11)

    transaction = {
        "txid": txid,
        "input_addresses": input_addresses,
        "output_addresses": output_addresses,
        "input_amounts": [_sats_to_btc(value) for value in input_sats],
        "output_amounts": [_sats_to_btc(value) for value in output_sats],
        "fee": _sats_to_btc(fee_sats),
        "script_type": generator.choice(_SCRIPT_TYPES),
    }
    # Every group spans the full timeline distribution. Class is sampled independently of time.
    base_time = _BASE_TIMESTAMP + timedelta(
        minutes=transaction_index * 11 + generator.randint(0, 90),
        seconds=generator.randint(0, 59),
    )
    rapid = "rapid_sequence_pattern" in active_tags
    shared = "shared_network_pattern" in active_tags
    shared_probability = {"weak": 0.45, "medium": 0.68, "strong": 0.86}.get(intensity, 0.25)
    if not shared:
        shared_probability = 0.18
    records: list[dict[str, Any]] = []
    observed_endpoints: list[tuple[str, str]] = []
    elapsed = 0
    source_anchor = generator.randrange(8)
    for observation_index in range(observation_count):
        region = generator.randrange(8)
        destination_region = (
            region if generator.random() < (0.64 if shared else 0.43) else generator.randrange(8)
        )
        reuse_slot = (
            source_anchor if generator.random() < shared_probability else generator.randrange(32)
        )
        src_ip = _group_ip(group_index, region, reuse_slot, source=True)
        dst_ip = _group_ip(group_index, destination_region, generator.randrange(32), source=False)
        if observation_index:
            if rapid:
                scale = {"weak": 100, "medium": 35, "strong": 8}.get(intensity, 80)
                elapsed += max(1, int(generator.expovariate(1 / scale)))
            else:
                # Natural bursts overlap rapid behaviour, while long gaps create nuisance spread.
                elapsed += (
                    generator.randint(3, 90)
                    if generator.random() < 0.24
                    else generator.randint(120, 4_800)
                )
        records.append(
            {
                **transaction,
                "timestamp": (base_time + timedelta(seconds=elapsed))
                .isoformat()
                .replace("+00:00", "Z"),
                "src_ip": src_ip,
                "dst_ip": dst_ip,
                "src_port": generator.choice(_COMMON_PORTS),
                "dst_port": generator.choice(_COMMON_PORTS),
                "geo_country": ("IN", "US", "DE", "SG")[region % 4],
                "asn": 64_500 + region % 5,
            }
        )
        observed_endpoints.append((src_ip, dst_ip))
    return records, {
        "txid": txid,
        "scenario_class": scenario,
        "scenario_group_id": group_id,
        "scenario_intensity": intensity,
        "secondary_tags": secondary_tags,
        "structural_truth": {
            "input_count": input_count,
            "output_count": output_count,
            "observation_count": observation_count,
            "observed_endpoints": observed_endpoints,
        },
    }


def _partition(total: int, weights: list[float]) -> list[int]:
    weight_sum = sum(weights)
    values = [max(1, int(total * weight / weight_sum)) for weight in weights]
    difference = total - sum(values)
    values[0] += difference
    return values


def _group_ip(group_index: int, region: int, slot: int, *, source: bool) -> str:
    # /48 identifies only an overlapping enrichment region; exact endpoints remain group-local.
    role = 1 if source else 2
    return f"2001:db8:{region + 1:x}:{group_index & 0xFFFF:x}:{role:x}::{slot + 1:x}"


def _permutation(count: int, seed: int) -> tuple[int, int]:
    if count == 1:
        return 1, 0
    candidate = 2 * (seed % max(1, count // 2)) + 1
    while math.gcd(candidate, count) != 1:
        candidate += 2
    return candidate % count, _seed(seed, "order", count) % count


def _longest_run(values: list[str]) -> int:
    longest = current = 0
    previous: str | None = None
    for value in values:
        current = current + 1 if value == previous else 1
        longest = max(longest, current)
        previous = value
    return longest


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
