from __future__ import annotations

import hashlib
import json
import logging
import random
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

import igraph as ig
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.cluster import HDBSCAN
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.analytics.validation import validate_analytical_dataset
from bitcoin_intel.entity.models import (
    BEHAVIORAL_FEATURE_COLUMNS,
    ENTITY_METHOD_VERSION,
    ENTITY_SCHEMA_VERSION,
    ENTITY_TABLES,
    MANIFEST_FILE_NAME,
    PART_FILE_NAME,
    EntityBuildConfig,
    EntityBuildSummary,
)
from bitcoin_intel.features.models import (
    FEATURE_SCHEMA_VERSION_V1,
    SUPPORTED_FEATURE_SCHEMA_VERSIONS,
    feature_tables_for_version,
)
from bitcoin_intel.features.models import (
    MANIFEST_FILE_NAME as FEATURE_MANIFEST_FILE_NAME,
)
from bitcoin_intel.features.models import (
    PART_FILE_NAME as FEATURE_PART_FILE_NAME,
)

_LOGGER = logging.getLogger(__name__)
_ROW_GROUP_SIZE = 65_536


class EntityBuildError(RuntimeError):
    """Raised when an entity-hypothesis store cannot be built safely."""


@dataclass(frozen=True, slots=True)
class OwnershipAnalysis:
    addresses: tuple[str, ...]
    input_addresses: dict[str, tuple[str, ...]]
    collaborative_rows: tuple[dict[str, Any], ...]
    raw_mapping: dict[str, str]
    suppressed_mapping: dict[str, str]
    accepted_pair_sources: dict[tuple[str, str], tuple[str, ...]]


class _UnionFind:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.parent = {value: value for value in values}
        self.rank = dict.fromkeys(values, 0)

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            parent = self.parent[value]
            self.parent[value] = root
            value = parent
        return root

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        left_rank = self.rank[left_root]
        right_rank = self.rank[right_root]
        if left_rank < right_rank or (left_rank == right_rank and left_root > right_root):
            left_root, right_root = right_root, left_root
            left_rank, right_rank = right_rank, left_rank
        self.parent[right_root] = left_root
        if left_rank == right_rank:
            self.rank[left_root] += 1

    def content_mapping(self, namespace: str) -> dict[str, str]:
        members: defaultdict[str, list[str]] = defaultdict(list)
        for value in sorted(self.parent):
            members[self.find(value)].append(value)
        result: dict[str, str] = {}
        for values in members.values():
            candidate_id = _content_id(namespace, values)
            result.update(dict.fromkeys(values, candidate_id))
        return result


def build_entity_hypotheses(
    dataset_path: Path,
    feature_path: Path,
    output_path: Path,
    config: EntityBuildConfig | None = None,
) -> EntityBuildSummary:
    effective_config = config or EntityBuildConfig()
    dataset = AnalyticalDataset(dataset_path)
    integrity = validate_analytical_dataset(dataset)
    if not integrity.is_valid:
        codes = ", ".join(issue.code for issue in integrity.issues)
        raise EntityBuildError(f"canonical dataset failed integrity validation: {codes}")
    feature_root, feature_manifest = _load_feature_store(feature_path, dataset)

    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise EntityBuildError(
            f"entity output already exists and will not be overwritten: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        analysis, transaction_outputs, address_source_ips = analyze_ownership(
            dataset, effective_config
        )
        evidence_rows, bridge_pairs = _build_evidence_rows(analysis, address_source_ips)
        candidate_rows, membership_rows = _build_candidate_rows(
            analysis, evidence_rows, bridge_pairs
        )
        behavioral_rows = _build_behavioral_communities(
            feature_root, analysis.addresses, effective_config
        )
        topological_rows = _build_topological_communities(
            analysis.addresses,
            analysis.input_addresses,
            transaction_outputs,
            effective_config.leiden_seed,
        )
        table_rows: dict[str, list[dict[str, Any]]] = {
            "candidate_entities": candidate_rows,
            "candidate_memberships": membership_rows,
            "ownership_evidence": evidence_rows,
            "collaborative_transactions": list(analysis.collaborative_rows),
            "behavioral_communities": behavioral_rows,
            "topological_communities": topological_rows,
        }
        table_metadata: dict[str, dict[str, Any]] = {}
        for name, definition in ENTITY_TABLES.items():
            rows = sorted(
                table_rows[name], key=lambda row: tuple(row[key] for key in definition.sort_by)
            )
            table = pa.Table.from_pylist(rows, schema=definition.schema)
            path = staging / name / PART_FILE_NAME
            path.parent.mkdir(parents=True, exist_ok=False)
            pq.write_table(
                table,
                path,
                compression="zstd",
                use_dictionary=True,
                write_statistics=True,
                row_group_size=_ROW_GROUP_SIZE,
                version="2.6",
            )
            table_metadata[name] = {
                "file": f"{name}/{PART_FILE_NAME}",
                "rows": table.num_rows,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }

        canonical_manifest_hash = _sha256_file(dataset.path / "manifest.json")
        feature_manifest_hash = _sha256_file(feature_root / FEATURE_MANIFEST_FILE_NAME)
        semantic_identity: dict[str, Any] = {
            "entity_schema_version": ENTITY_SCHEMA_VERSION,
            "entity_method_version": ENTITY_METHOD_VERSION,
            "canonical_schema_version": dataset.manifest.schema_version,
            "canonical_manifest_sha256": canonical_manifest_hash,
            "feature_schema_version": feature_manifest["feature_schema_version"],
            "feature_dataset_id": feature_manifest["feature_dataset_id"],
            "feature_manifest_sha256": feature_manifest_hash,
            "build_configuration": {
                **effective_config.semantic_dict(),
                "ownership_rule": "multi-input co-spend with suspected-collaboration suppression",
                "strong_edge_encoding": "lexicographic star per transaction",
                "network_evidence_policy": "supporting-only; never passed to union-find",
                "behavioral_features": list(BEHAVIORAL_FEATURE_COLUMNS),
                "behavioral_preprocessing": "median imputation then standard scaling",
                "topological_projection": (
                    "undirected all-pairs address co-occurrence per transaction"
                ),
            },
        }
        entity_dataset_id = _sha256_bytes(_canonical_json(semantic_identity))
        manifest = {
            "entity_dataset_id": entity_dataset_id,
            **semantic_identity,
            "built_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "inference_disclaimer": (
                "Candidate entities and communities are analytical hypotheses, not facts about "
                "wallets, people, ownership, control, guilt, or criminality."
            ),
            "output_tables": table_metadata,
        }
        (staging / MANIFEST_FILE_NAME).write_bytes(_pretty_json(manifest))

        from bitcoin_intel.entity.validation import validate_entity_store

        report = validate_entity_store(staging, dataset.path, feature_root)
        if not report.is_valid:
            details = "; ".join(f"{issue.code}={issue.count}" for issue in report.issues)
            raise EntityBuildError(f"staged entity store failed validation: {details}")
        if destination.exists():
            raise EntityBuildError(
                f"entity output was created concurrently and will not be overwritten: {destination}"
            )
        staging.replace(destination)
        return EntityBuildSummary(
            output_path=destination,
            entity_dataset_id=entity_dataset_id,
            candidate_entity_count=len(candidate_rows),
            address_count=len(analysis.addresses),
            suppressed_transaction_count=sum(
                bool(row["collaborative_tx_suspected"]) for row in analysis.collaborative_rows
            ),
            behavioral_noise_count=sum(bool(row["is_noise"]) for row in behavioral_rows),
            table_rows={name: len(rows) for name, rows in table_rows.items()},
        )
    except Exception:
        _LOGGER.exception("entity-hypothesis build failed: dataset=%s", dataset.path)
        if staging.exists():
            shutil.rmtree(staging)
        raise


def analyze_ownership(
    dataset: AnalyticalDataset, config: EntityBuildConfig
) -> tuple[OwnershipAnalysis, dict[str, tuple[str, ...]], dict[str, frozenset[str]]]:
    input_rows: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    output_rows: defaultdict[str, list[tuple[int, str, int]]] = defaultdict(list)
    address_txids: defaultdict[str, set[str]] = defaultdict(set)
    tx_source_ips: defaultdict[str, set[str]] = defaultdict(set)
    with dataset.connect() as connection:
        for txid, index, address in connection.execute(
            "SELECT txid, input_index, address FROM transaction_inputs ORDER BY txid, input_index"
        ).fetchall():
            input_rows[str(txid)].append((int(index), str(address)))
            address_txids[str(address)].add(str(txid))
        for txid, index, address, amount in connection.execute(
            "SELECT txid, output_index, address, amount_sats "
            "FROM transaction_outputs ORDER BY txid, output_index"
        ).fetchall():
            output_rows[str(txid)].append((int(index), str(address), int(amount)))
            address_txids[str(address)].add(str(txid))
        for txid, source_ip in connection.execute(
            "SELECT DISTINCT txid, src_ip FROM network_observations ORDER BY txid, src_ip"
        ).fetchall():
            tx_source_ips[str(txid)].add(str(source_ip))

    addresses = tuple(sorted(address_txids))
    normalized_inputs = {
        txid: tuple(sorted({address for _, address in rows}))
        for txid, rows in sorted(input_rows.items())
    }
    normalized_outputs = {
        txid: tuple(sorted({address for _, address, _ in rows}))
        for txid, rows in sorted(output_rows.items())
    }
    raw = _UnionFind(addresses)
    suppressed = _UnionFind(addresses)
    accepted_sources: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    collaborative_rows: list[dict[str, Any]] = []
    for txid in sorted(set(input_rows) | set(output_rows)):
        inputs = normalized_inputs.get(txid, ())
        output_details = output_rows.get(txid, [])
        detector_row = _collaborative_row(txid, inputs, output_details, config)
        collaborative_rows.append(detector_row)
        pairs = _star_pairs(inputs)
        for left, right in pairs:
            raw.union(left, right)
            if not detector_row["collaborative_tx_suspected"]:
                suppressed.union(left, right)
                accepted_sources[(left, right)].append(txid)

    address_source_ips = {
        address: frozenset(
            source_ip for txid in txids for source_ip in tx_source_ips.get(txid, set())
        )
        for address, txids in address_txids.items()
    }
    return (
        OwnershipAnalysis(
            addresses=addresses,
            input_addresses=normalized_inputs,
            collaborative_rows=tuple(collaborative_rows),
            raw_mapping=raw.content_mapping("raw-mih-v1"),
            suppressed_mapping=suppressed.content_mapping(
                f"{ENTITY_SCHEMA_VERSION}:{ENTITY_METHOD_VERSION}"
            ),
            accepted_pair_sources={
                pair: tuple(sorted(sources)) for pair, sources in sorted(accepted_sources.items())
            },
        ),
        normalized_outputs,
        address_source_ips,
    )


def _collaborative_row(
    txid: str,
    inputs: tuple[str, ...],
    outputs: list[tuple[int, str, int]],
    config: EntityBuildConfig,
) -> dict[str, Any]:
    amounts = [amount for _, _, amount in outputs]
    counts = Counter(amounts)
    equal_groups = sum(count >= 2 for count in counts.values())
    maximum_equal = max(counts.values(), default=0)
    equal_fraction = maximum_equal / len(amounts) if amounts else 0.0
    mean = sum(amounts) / len(amounts) if amounts else 0.0
    balanced = (
        sum(mean * 0.5 <= amount <= mean * 1.5 for amount in amounts) / len(amounts)
        if mean
        else 0.0
    )
    suspected = (
        len(inputs) >= config.collaborative_min_inputs
        and len({address for _, address, _ in outputs}) >= config.collaborative_min_outputs
        and maximum_equal >= config.collaborative_min_equal_outputs
        and equal_fraction >= config.collaborative_min_equal_fraction
    )
    return {
        "txid": txid,
        "distinct_input_count": len(inputs),
        "distinct_output_count": len({address for _, address, _ in outputs}),
        "equal_output_group_count": equal_groups,
        "max_equal_output_multiplicity": maximum_equal,
        "equal_output_fraction": float(equal_fraction),
        "balanced_output_fraction": float(balanced),
        "collaborative_tx_suspected": suspected,
        "suppression_reason": (
            "multi-party-like input/output counts with repeated equal output values"
            if suspected
            else None
        ),
    }


def _star_pairs(addresses: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if len(addresses) < 2:
        return ()
    anchor = addresses[0]
    return tuple((anchor, address) for address in addresses[1:])


def _build_evidence_rows(
    analysis: OwnershipAnalysis, address_source_ips: dict[str, frozenset[str]]
) -> tuple[list[dict[str, Any]], frozenset[tuple[str, str]]]:
    all_strong: defaultdict[tuple[str, str], list[tuple[str, bool, str | None]]] = defaultdict(list)
    suspected = {
        str(row["txid"]): bool(row["collaborative_tx_suspected"])
        for row in analysis.collaborative_rows
    }
    for txid, addresses in sorted(analysis.input_addresses.items()):
        for pair in _star_pairs(addresses):
            is_suppressed = suspected.get(txid, False)
            all_strong[pair].append(
                (
                    txid,
                    is_suppressed,
                    "suspected collaborative transaction" if is_suppressed else None,
                )
            )
    bridge_pairs = _fragile_bridges(analysis.addresses, analysis.accepted_pair_sources)
    rows: list[dict[str, Any]] = []
    for pair, sources in sorted(all_strong.items()):
        for txid, is_suppressed, reason in sources:
            rows.append(
                _evidence_row(
                    pair,
                    "MULTI_INPUT_CO_SPEND",
                    txid,
                    "STRONG",
                    1.0,
                    not is_suppressed,
                    is_suppressed,
                    reason,
                    pair in bridge_pairs and len(analysis.accepted_pair_sources.get(pair, ())) == 1,
                )
            )
    ip_addresses: defaultdict[str, list[str]] = defaultdict(list)
    for address, source_ips in sorted(address_source_ips.items()):
        for source_ip in sorted(source_ips):
            ip_addresses[source_ip].append(address)
    for source_ip, ip_members in sorted(ip_addresses.items()):
        for pair in _star_pairs(tuple(sorted(set(ip_members)))):
            source_id = _content_id("shared-source-ip-v1", [source_ip])
            rows.append(
                _evidence_row(
                    pair,
                    "SHARED_SOURCE_IP",
                    source_id,
                    "SUPPORTING",
                    1.0,
                    False,
                    False,
                    None,
                    False,
                )
            )
    return rows, bridge_pairs


def _evidence_row(
    pair: tuple[str, str],
    evidence_type: str,
    source_id: str,
    strength: str,
    support_value: float,
    selected: bool,
    suppressed: bool,
    reason: str | None,
    fragile: bool,
) -> dict[str, Any]:
    evidence_id = _content_id("ownership-evidence-v1", [pair[0], pair[1], evidence_type, source_id])
    return {
        "evidence_id": evidence_id,
        "address_a": pair[0],
        "address_b": pair[1],
        "evidence_type": evidence_type,
        "evidence_source_id": source_id,
        "strength_class": strength,
        "support_value": support_value,
        "merge_selected": selected,
        "suppressed": suppressed,
        "suppression_reason": reason,
        "fragile_bridge": fragile,
    }


def _fragile_bridges(
    addresses: tuple[str, ...], pair_sources: dict[tuple[str, str], tuple[str, ...]]
) -> frozenset[tuple[str, str]]:
    if not pair_sources:
        return frozenset()
    index = {address: position for position, address in enumerate(addresses)}
    pairs = sorted(pair_sources)
    graph = ig.Graph(
        n=len(addresses), edges=[(index[a], index[b]) for a, b in pairs], directed=False
    )
    return frozenset(
        pairs[edge_id] for edge_id in graph.bridges() if len(pair_sources[pairs[edge_id]]) == 1
    )


def _build_candidate_rows(
    analysis: OwnershipAnalysis,
    evidence_rows: list[dict[str, Any]],
    bridge_pairs: frozenset[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for address, candidate_id in analysis.suppressed_mapping.items():
        groups[candidate_id].append(address)
    selected_pairs = set(analysis.accepted_pair_sources)
    supporting_by_candidate: Counter[str] = Counter()
    for row in evidence_rows:
        if row["strength_class"] == "SUPPORTING":
            left_candidate = analysis.suppressed_mapping[str(row["address_a"])]
            right_candidate = analysis.suppressed_mapping[str(row["address_b"])]
            supporting_by_candidate[left_candidate] += 1
            if right_candidate != left_candidate:
                supporting_by_candidate[right_candidate] += 1
    candidate_rows: list[dict[str, Any]] = []
    membership_rows: list[dict[str, Any]] = []
    for candidate_id, raw_members in sorted(groups.items()):
        members = sorted(raw_members)
        member_set = set(members)
        edges = sorted(pair for pair in selected_pairs if set(pair) <= member_set)
        degrees: Counter[str] = Counter(endpoint for pair in edges for endpoint in pair)
        anchor = members[0]
        direct_to_anchor = {
            member: member == anchor or tuple(sorted((anchor, member))) in selected_pairs
            for member in members
        }
        transitive_count = sum(not direct for direct in direct_to_anchor.values())
        possible_edges = len(members) * (len(members) - 1) / 2
        bridge_count = sum(pair in bridge_pairs for pair in edges)
        robustness = 1.0 if not edges else 1.0 - bridge_count / len(edges)
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "member_count": len(members),
                "strong_evidence_edge_count": len(edges),
                "supporting_evidence_count": supporting_by_candidate[candidate_id],
                "merge_transaction_count": len(
                    {txid for pair in edges for txid in analysis.accepted_pair_sources[pair]}
                ),
                "fragile_bridge_count": bridge_count,
                "edge_density": len(edges) / possible_edges if possible_edges else 0.0,
                "minimum_degree": min((degrees[member] for member in members), default=0),
                "transitive_only_member_count": transitive_count,
                "robustness_score": robustness,
                "method_version": ENTITY_METHOD_VERSION,
            }
        )
        transaction_total = max(
            1, len({txid for pair in edges for txid in analysis.accepted_pair_sources[pair]})
        )
        for member in members:
            incident_sources = {
                txid
                for pair in edges
                if member in pair
                for txid in analysis.accepted_pair_sources[pair]
            }
            membership_rows.append(
                {
                    "candidate_id": candidate_id,
                    "address": member,
                    "membership_support": (
                        1.0 if len(members) == 1 else len(incident_sources) / transaction_total
                    ),
                    "direct_to_anchor": direct_to_anchor[member],
                    "transitive_only": not direct_to_anchor[member],
                    "accepted_evidence_count": len(incident_sources),
                }
            )
    return candidate_rows, membership_rows


def _build_behavioral_communities(
    feature_root: Path, addresses: tuple[str, ...], config: EntityBuildConfig
) -> list[dict[str, Any]]:
    columns = ["address", *BEHAVIORAL_FEATURE_COLUMNS]
    table = pq.read_table(
        feature_root / "address_features" / FEATURE_PART_FILE_NAME, columns=columns
    )
    by_address = {str(row["address"]): row for row in table.to_pylist()}
    missing = sorted(set(addresses) - set(by_address))
    if missing:
        raise EntityBuildError(f"address feature coverage is incomplete: {missing[0]}")
    values = np.asarray(
        [
            [
                float(by_address[address][column])
                if by_address[address][column] is not None
                else np.nan
                for column in BEHAVIORAL_FEATURE_COLUMNS
            ]
            for address in addresses
        ],
        dtype=np.float64,
    )
    if len(addresses) < config.behavioral_min_cluster_size:
        labels = np.full(len(addresses), -1, dtype=np.int64)
    else:
        pipeline = make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            HDBSCAN(
                min_cluster_size=config.behavioral_min_cluster_size,
                min_samples=config.behavioral_min_samples,
                allow_single_cluster=False,
                copy=True,
            ),
        )
        labels = np.asarray(pipeline.fit_predict(values), dtype=np.int64)
    communities: defaultdict[int, list[str]] = defaultdict(list)
    for address, label in zip(addresses, labels, strict=True):
        if label >= 0:
            communities[int(label)].append(address)
    ids = {
        label: _content_id("behavioral-hdbscan-v1", members)
        for label, members in communities.items()
    }
    return [
        {
            "address": address,
            "behavioral_community_id": ids.get(int(label)),
            "community_size": len(communities[int(label)]) if label >= 0 else 0,
            "is_noise": bool(label < 0),
            "algorithm": "hdbscan-median-impute-standard-scale-v1",
        }
        for address, label in zip(addresses, labels, strict=True)
    ]


def _build_topological_communities(
    addresses: tuple[str, ...],
    inputs: dict[str, tuple[str, ...]],
    outputs: dict[str, tuple[str, ...]],
    seed: int,
) -> list[dict[str, Any]]:
    index = {address: position for position, address in enumerate(addresses)}
    weights: Counter[tuple[str, str]] = Counter()
    for txid in sorted(set(inputs) | set(outputs)):
        participants = sorted(set(inputs.get(txid, ())) | set(outputs.get(txid, ())))
        weights.update(combinations(participants, 2))
    pairs = sorted(weights)
    graph = ig.Graph(
        n=len(addresses),
        edges=[(index[left], index[right]) for left, right in pairs],
        directed=False,
    )
    graph.vs["name"] = list(addresses)
    if pairs:
        ig.set_random_number_generator(random.Random(seed))
        partition = graph.community_leiden(
            objective_function="modularity",
            weights=[weights[pair] for pair in pairs],
            n_iterations=2,
        )
        membership = list(partition.membership)
    else:
        membership = list(range(len(addresses)))
    communities: defaultdict[int, list[str]] = defaultdict(list)
    for address, label in zip(addresses, membership, strict=True):
        communities[int(label)].append(address)
    ids = {
        label: _content_id("topological-leiden-v1", members)
        for label, members in communities.items()
    }
    return [
        {
            "address": address,
            "topological_community_id": ids[int(label)],
            "community_size": len(communities[int(label)]),
            "algorithm": "leiden-modularity-weighted-v1",
        }
        for address, label in zip(addresses, membership, strict=True)
    ]


def _load_feature_store(
    feature_path: Path, dataset: AnalyticalDataset
) -> tuple[Path, dict[str, Any]]:
    try:
        root = feature_path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise EntityBuildError(f"feature path is not a directory: {root}")
        manifest = json.loads((root / FEATURE_MANIFEST_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EntityBuildError(f"feature manifest is unreadable or malformed: {error}") from error
    if not isinstance(manifest, dict):
        raise EntityBuildError("feature manifest must be a JSON object")
    version = manifest.get("feature_schema_version")
    if version not in SUPPORTED_FEATURE_SCHEMA_VERSIONS or not isinstance(version, str):
        raise EntityBuildError("feature schema version is unsupported")
    if manifest.get("canonical_manifest_sha256") != _sha256_file(dataset.path / "manifest.json"):
        raise EntityBuildError("feature store was not derived from the supplied canonical dataset")
    raw_table = manifest.get("output_tables", {}).get("address_features")
    if (
        not isinstance(raw_table, dict)
        or raw_table.get("file") != "address_features/part-00000.parquet"
    ):
        raise EntityBuildError("address feature metadata is missing or malformed")
    path = root / "address_features" / FEATURE_PART_FILE_NAME
    if raw_table.get("sha256") != _sha256_file(path):
        raise EntityBuildError("address feature file differs from its manifest")
    expected = feature_tables_for_version(version)["address_features"].schema
    if not pq.read_schema(path).equals(expected, check_metadata=False):
        raise EntityBuildError("address feature schema is unsupported")
    if version == FEATURE_SCHEMA_VERSION_V1 and not isinstance(
        manifest.get("feature_dataset_id"), str
    ):
        raise EntityBuildError("feature dataset identity is missing")
    return root, manifest


def _content_id(namespace: str, values: list[str] | tuple[str, ...]) -> str:
    payload = json.dumps([namespace, *sorted(values)], separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
