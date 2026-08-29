from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.entity.models import (
    ENTITY_METHOD_VERSION,
    ENTITY_SCHEMA_VERSION,
    ENTITY_TABLES,
    MANIFEST_FILE_NAME,
    PART_FILE_NAME,
    EntityValidationIssue,
    EntityValidationReport,
)
from bitcoin_intel.features.models import MANIFEST_FILE_NAME as FEATURE_MANIFEST_FILE_NAME


class EntityStoreError(RuntimeError):
    """Raised when entity-hypothesis metadata cannot be interpreted safely."""


def validate_entity_store(
    entity_path: Path, dataset_path: Path, feature_path: Path
) -> EntityValidationReport:
    root = _resolve_directory(entity_path)
    dataset = AnalyticalDataset(dataset_path)
    feature_root = _resolve_directory(feature_path)
    manifest = _load_json(root / MANIFEST_FILE_NAME, "entity manifest")
    feature_manifest = _load_json(feature_root / FEATURE_MANIFEST_FILE_NAME, "feature manifest")
    issues: list[EntityValidationIssue] = []
    _mismatch(
        issues,
        "ENTITY_SCHEMA_VERSION_MISMATCH",
        manifest.get("entity_schema_version") != ENTITY_SCHEMA_VERSION,
        "entity schema version is unsupported",
    )
    _mismatch(
        issues,
        "ENTITY_METHOD_VERSION_MISMATCH",
        manifest.get("entity_method_version") != ENTITY_METHOD_VERSION,
        "entity method version is unsupported",
    )
    _mismatch(
        issues,
        "CANONICAL_LINEAGE_MISMATCH",
        manifest.get("canonical_manifest_sha256") != _sha256_file(dataset.path / "manifest.json"),
        "entity store was not derived from the supplied canonical dataset",
    )
    feature_manifest_path = feature_root / FEATURE_MANIFEST_FILE_NAME
    _mismatch(
        issues,
        "FEATURE_LINEAGE_MISMATCH",
        manifest.get("feature_manifest_sha256") != _sha256_file(feature_manifest_path)
        or manifest.get("feature_dataset_id") != feature_manifest.get("feature_dataset_id")
        or manifest.get("feature_schema_version") != feature_manifest.get("feature_schema_version"),
        "entity store was not derived from the supplied feature store",
    )
    semantic_keys = (
        "entity_schema_version",
        "entity_method_version",
        "canonical_schema_version",
        "canonical_manifest_sha256",
        "feature_schema_version",
        "feature_dataset_id",
        "feature_manifest_sha256",
        "build_configuration",
    )
    semantic_identity = {key: manifest.get(key) for key in semantic_keys}
    expected_id = hashlib.sha256(
        json.dumps(semantic_identity, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    _mismatch(
        issues,
        "ENTITY_DATASET_ID_MISMATCH",
        manifest.get("entity_dataset_id") != expected_id,
        "entity dataset semantic identity is invalid",
    )

    raw_tables = manifest.get("output_tables")
    if not isinstance(raw_tables, dict) or set(raw_tables) != set(ENTITY_TABLES):
        raise EntityStoreError("manifest output_tables does not match the entity table contract")
    loaded: dict[str, list[dict[str, Any]]] = {}
    queryable = True
    for name, definition in ENTITY_TABLES.items():
        raw = raw_tables.get(name)
        if not isinstance(raw, dict):
            raise EntityStoreError(f"manifest table entry is malformed: {name}")
        relative = Path(name) / PART_FILE_NAME
        if raw.get("file") != relative.as_posix():
            raise EntityStoreError(f"manifest table path is unsupported: {name}")
        path = _resolve_child(root, relative)
        try:
            parquet = pq.ParquetFile(path)
            table = parquet.read()
        except (OSError, ValueError) as error:
            queryable = False
            issues.append(EntityValidationIssue("UNREADABLE_ENTITY_PARQUET", 1, f"{name}: {error}"))
            continue
        schema_mismatch = not table.schema.equals(definition.schema, check_metadata=False)
        _mismatch(
            issues,
            "ENTITY_TABLE_SCHEMA_MISMATCH",
            schema_mismatch,
            f"{name} has an unexpected Parquet schema",
        )
        queryable = queryable and not schema_mismatch
        _mismatch(
            issues,
            "ENTITY_ROW_COUNT_MISMATCH",
            raw.get("rows") != table.num_rows,
            f"{name} row count differs from the manifest",
        )
        _mismatch(
            issues,
            "ENTITY_FILE_SIZE_MISMATCH",
            raw.get("bytes") != path.stat().st_size,
            f"{name} byte size differs from the manifest",
        )
        _mismatch(
            issues,
            "ENTITY_FILE_HASH_MISMATCH",
            raw.get("sha256") != _sha256_file(path),
            f"{name} SHA-256 differs from the manifest",
        )
        loaded[name] = table.to_pylist()
    if queryable and len(loaded) == len(ENTITY_TABLES):
        _validate_values(loaded, dataset, manifest, issues)
    return EntityValidationReport(tuple(issues))


def _validate_values(
    tables: dict[str, list[dict[str, Any]]],
    dataset: AnalyticalDataset,
    manifest: dict[str, Any],
    issues: list[EntityValidationIssue],
) -> None:
    canonical_inputs: defaultdict[str, set[str]] = defaultdict(set)
    canonical_address_txids: defaultdict[str, set[str]] = defaultdict(set)
    tx_source_ips: defaultdict[str, set[str]] = defaultdict(set)
    with dataset.connect() as connection:
        canonical_addresses = {
            str(row[0])
            for row in connection.execute(
                "SELECT address FROM transaction_inputs "
                "UNION SELECT address FROM transaction_outputs"
            ).fetchall()
        }
        canonical_txids = {
            str(row[0]) for row in connection.execute("SELECT txid FROM transactions").fetchall()
        }
        for txid, address in connection.execute(
            "SELECT txid, address FROM transaction_inputs"
        ).fetchall():
            canonical_inputs[str(txid)].add(str(address))
            canonical_address_txids[str(address)].add(str(txid))
        for txid, address in connection.execute(
            "SELECT txid, address FROM transaction_outputs"
        ).fetchall():
            canonical_address_txids[str(address)].add(str(txid))
        for txid, source_ip in connection.execute(
            "SELECT DISTINCT txid, src_ip FROM network_observations"
        ).fetchall():
            tx_source_ips[str(txid)].add(str(source_ip))
    memberships = tables["candidate_memberships"]
    entities = tables["candidate_entities"]
    evidence = tables["ownership_evidence"]
    collaborative = tables["collaborative_transactions"]
    behavioral = tables["behavioral_communities"]
    topological = tables["topological_communities"]

    membership_addresses = [str(row["address"]) for row in memberships]
    _mismatch(
        issues,
        "MEMBERSHIP_ADDRESS_COVERAGE_MISMATCH",
        set(membership_addresses) != canonical_addresses,
        "candidate memberships must cover every canonical address exactly once",
    )
    _mismatch(
        issues,
        "DUPLICATE_MEMBERSHIP",
        len(membership_addresses) != len(set(membership_addresses)),
        "an address has more than one candidate membership",
    )
    groups: defaultdict[str, list[str]] = defaultdict(list)
    membership_rows_by_candidate: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in memberships:
        candidate_id = str(row["candidate_id"])
        groups[candidate_id].append(str(row["address"]))
        membership_rows_by_candidate[candidate_id].append(row)
        invalid_flags = bool(row["direct_to_anchor"]) == bool(row["transitive_only"])
        invalid_support = not 0.0 <= float(row["membership_support"]) <= 1.0
        _mismatch(
            issues,
            "INVALID_MEMBERSHIP_METADATA",
            invalid_flags or invalid_support,
            "membership support or direct/transitive flags are invalid",
        )
    entity_by_id = {str(row["candidate_id"]): row for row in entities}
    _mismatch(
        issues,
        "CANDIDATE_SET_MISMATCH",
        set(groups) != set(entity_by_id) or len(entity_by_id) != len(entities),
        "candidate rows and membership candidate IDs differ",
    )
    for candidate_id, members in groups.items():
        expected_id = _content_id(f"{ENTITY_SCHEMA_VERSION}:{ENTITY_METHOD_VERSION}", members)
        entity_row = entity_by_id.get(candidate_id)
        _mismatch(
            issues,
            "CANDIDATE_ID_MISMATCH",
            candidate_id != expected_id,
            "candidate ID is not derived from its sorted membership",
        )
        if entity_row is not None:
            invalid = (
                int(entity_row["member_count"]) != len(members)
                or not 0.0 <= float(entity_row["edge_density"]) <= 1.0
                or not 0.0 <= float(entity_row["robustness_score"]) <= 1.0
                or str(entity_row["method_version"]) != ENTITY_METHOD_VERSION
            )
            _mismatch(
                issues,
                "INVALID_CANDIDATE_DIAGNOSTICS",
                invalid,
                "candidate counts, scores, or method metadata are invalid",
            )

    evidence_ids = [str(row["evidence_id"]) for row in evidence]
    _mismatch(
        issues,
        "DUPLICATE_EVIDENCE_ID",
        len(evidence_ids) != len(set(evidence_ids)),
        "ownership evidence IDs must be unique",
    )
    selected_edges: list[tuple[str, str]] = []
    selected_pair_sources: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    selected_pairs_by_candidate: defaultdict[str, set[tuple[str, str]]] = defaultdict(set)
    supporting_rows: set[tuple[str, str, str]] = set()
    supporting_by_candidate: Counter[str] = Counter()
    suppressed_txids = {
        str(row["txid"]) for row in collaborative if row["collaborative_tx_suspected"]
    }
    stored_mapping = {str(row["address"]): str(row["candidate_id"]) for row in memberships}
    for row in evidence:
        left, right = str(row["address_a"]), str(row["address_b"])
        selected = bool(row["merge_selected"])
        supporting = row["strength_class"] == "SUPPORTING"
        invalid = (
            left >= right
            or left not in canonical_addresses
            or right not in canonical_addresses
            or (supporting and selected)
            or (selected and bool(row["suppressed"]))
            or (selected and row["strength_class"] != "STRONG")
            or (
                row["evidence_type"] == "MULTI_INPUT_CO_SPEND"
                and row["evidence_source_id"] in suppressed_txids
                and selected
            )
        )
        expected_evidence_id = _content_id(
            "ownership-evidence-v1",
            [left, right, str(row["evidence_type"]), str(row["evidence_source_id"])],
        )
        invalid = invalid or str(row["evidence_id"]) != expected_evidence_id
        _mismatch(
            issues,
            "INVALID_OWNERSHIP_EVIDENCE",
            invalid,
            "evidence ordering, address references, or merge policy is invalid",
        )
        if selected:
            selected_edges.append((left, right))
            selected_pair_sources[(left, right)].add(str(row["evidence_source_id"]))
            selected_pairs_by_candidate[stored_mapping[left]].add((left, right))
        if supporting:
            supporting_rows.add((left, right, str(row["evidence_source_id"])))
            left_candidate = stored_mapping.get(left)
            right_candidate = stored_mapping.get(right)
            if left_candidate is not None:
                supporting_by_candidate[left_candidate] += 1
            if right_candidate is not None and right_candidate != left_candidate:
                supporting_by_candidate[right_candidate] += 1
    reconstructed = _components(canonical_addresses, selected_edges)
    _mismatch(
        issues,
        "MEMBERSHIP_EVIDENCE_MISMATCH",
        reconstructed != stored_mapping,
        "candidate memberships are not exactly reproduced by selected strong evidence",
    )

    expected_strong: set[tuple[str, str, str, bool, bool]] = set()
    collaborative_by_tx = {
        str(row["txid"]): bool(row["collaborative_tx_suspected"]) for row in collaborative
    }
    for txid, input_set in canonical_inputs.items():
        addresses = sorted(input_set)
        for right in addresses[1:]:
            suppressed = collaborative_by_tx.get(txid, False)
            expected_strong.add((addresses[0], right, txid, not suppressed, suppressed))
    actual_strong = {
        (
            str(row["address_a"]),
            str(row["address_b"]),
            str(row["evidence_source_id"]),
            bool(row["merge_selected"]),
            bool(row["suppressed"]),
        )
        for row in evidence
        if row["evidence_type"] == "MULTI_INPUT_CO_SPEND"
    }
    _mismatch(
        issues,
        "STRONG_EVIDENCE_COVERAGE_MISMATCH",
        actual_strong != expected_strong,
        "strong evidence does not exactly encode canonical multi-input stars and suppression",
    )
    address_source_ips: defaultdict[str, set[str]] = defaultdict(set)
    for address, txids in canonical_address_txids.items():
        for txid in txids:
            address_source_ips[address].update(tx_source_ips.get(txid, set()))
    ip_addresses: defaultdict[str, list[str]] = defaultdict(list)
    for address, source_ips in address_source_ips.items():
        for source_ip in source_ips:
            ip_addresses[source_ip].append(address)
    expected_supporting = {
        (addresses[0], right, _content_id("shared-source-ip-v1", [source_ip]))
        for source_ip, raw_addresses in ip_addresses.items()
        for addresses in [sorted(set(raw_addresses))]
        for right in addresses[1:]
    }
    _mismatch(
        issues,
        "SUPPORTING_EVIDENCE_COVERAGE_MISMATCH",
        supporting_rows != expected_supporting,
        "supporting evidence does not exactly encode bounded shared-source-IP stars",
    )

    fragile_pairs = {
        (str(row["address_a"]), str(row["address_b"]))
        for row in evidence
        if row["fragile_bridge"] and bool(row["merge_selected"])
    }
    for candidate_id, members in groups.items():
        member_set = set(members)
        pairs = selected_pairs_by_candidate[candidate_id]
        if any(pair[0] not in member_set or pair[1] not in member_set for pair in pairs):
            raise EntityStoreError("selected evidence crosses reconstructed candidate membership")
        degrees: Counter[str] = Counter(endpoint for pair in pairs for endpoint in pair)
        anchor = min(members)
        transitive = sum(
            member != anchor and tuple(sorted((anchor, member))) not in pairs for member in members
        )
        merge_transactions = {source for pair in pairs for source in selected_pair_sources[pair]}
        possible_edges = len(members) * (len(members) - 1) / 2
        bridge_count = sum(pair in fragile_pairs for pair in pairs)
        expected_density = len(pairs) / possible_edges if possible_edges else 0.0
        expected_robustness = 1.0 if not pairs else 1.0 - bridge_count / len(pairs)
        entity_row = entity_by_id[candidate_id]
        invalid = (
            int(entity_row["strong_evidence_edge_count"]) != len(pairs)
            or int(entity_row["supporting_evidence_count"]) != supporting_by_candidate[candidate_id]
            or int(entity_row["merge_transaction_count"]) != len(merge_transactions)
            or int(entity_row["fragile_bridge_count"]) != bridge_count
            or int(entity_row["minimum_degree"])
            != min((degrees[member] for member in members), default=0)
            or int(entity_row["transitive_only_member_count"]) != transitive
            or abs(float(entity_row["edge_density"]) - expected_density) > 1e-12
            or abs(float(entity_row["robustness_score"]) - expected_robustness) > 1e-12
        )
        _mismatch(
            issues,
            "CANDIDATE_DIAGNOSTIC_MISMATCH",
            invalid,
            "candidate diagnostics do not match selected evidence and memberships",
        )

        candidate_transactions = max(1, len(merge_transactions))
        for membership in membership_rows_by_candidate[candidate_id]:
            address = str(membership["address"])
            incident = {
                source
                for pair in pairs
                if address in pair
                for source in selected_pair_sources[pair]
            }
            direct = address == anchor or tuple(sorted((anchor, address))) in pairs
            expected_support = 1.0 if len(members) == 1 else len(incident) / candidate_transactions
            invalid_membership = (
                bool(membership["direct_to_anchor"]) != direct
                or bool(membership["transitive_only"]) == direct
                or int(membership["accepted_evidence_count"]) != len(incident)
                or abs(float(membership["membership_support"]) - expected_support) > 1e-12
            )
            _mismatch(
                issues,
                "MEMBERSHIP_DIAGNOSTIC_MISMATCH",
                invalid_membership,
                "membership diagnostics do not match selected evidence",
            )

    collaborative_txids = [str(row["txid"]) for row in collaborative]
    _mismatch(
        issues,
        "COLLABORATIVE_TRANSACTION_COVERAGE_MISMATCH",
        set(collaborative_txids) != canonical_txids
        or len(collaborative_txids) != len(set(collaborative_txids)),
        "collaborative diagnostics must cover every canonical transaction exactly once",
    )
    raw_config = manifest.get("build_configuration")
    if not isinstance(raw_config, dict):
        raise EntityStoreError("entity build_configuration is malformed")
    detector_errors = 0
    for row in collaborative:
        expected_suspected = (
            int(row["distinct_input_count"]) >= int(raw_config["collaborative_min_inputs"])
            and int(row["distinct_output_count"]) >= int(raw_config["collaborative_min_outputs"])
            and int(row["max_equal_output_multiplicity"])
            >= int(raw_config["collaborative_min_equal_outputs"])
            and float(row["equal_output_fraction"])
            >= float(raw_config["collaborative_min_equal_fraction"])
        )
        detector_errors += bool(row["collaborative_tx_suspected"]) != expected_suspected
        detector_errors += bool(row["suppression_reason"]) != expected_suspected
    _mismatch(
        issues,
        "COLLABORATIVE_DETECTOR_MISMATCH",
        detector_errors > 0,
        "collaborative decisions do not match the manifest detector configuration",
        detector_errors,
    )
    for name, rows, id_column, nullable_noise in (
        ("behavioral", behavioral, "behavioral_community_id", True),
        ("topological", topological, "topological_community_id", False),
    ):
        row_addresses = [str(row["address"]) for row in rows]
        _mismatch(
            issues,
            "COMMUNITY_ADDRESS_COVERAGE_MISMATCH",
            set(row_addresses) != canonical_addresses
            or len(row_addresses) != len(set(row_addresses)),
            f"{name} communities must cover every canonical address exactly once",
        )
        if nullable_noise:
            invalid_noise = sum(
                bool(row["is_noise"])
                != (row[id_column] is None and int(row["community_size"]) == 0)
                for row in rows
            )
            _mismatch(
                issues,
                "INVALID_BEHAVIORAL_NOISE_ENCODING",
                invalid_noise > 0,
                "HDBSCAN noise must be preserved with a null community ID and size zero",
                invalid_noise,
            )
        community_members: defaultdict[str, list[str]] = defaultdict(list)
        for row in rows:
            community_id = row[id_column]
            if community_id is not None:
                community_members[str(community_id)].append(str(row["address"]))
        namespace = "behavioral-hdbscan-v1" if nullable_noise else "topological-leiden-v1"
        invalid_communities = 0
        for community_id, members in community_members.items():
            invalid_communities += community_id != _content_id(namespace, members)
        invalid_communities += sum(
            row[id_column] is not None
            and int(row["community_size"]) != len(community_members[str(row[id_column])])
            for row in rows
        )
        _mismatch(
            issues,
            "COMMUNITY_ID_OR_SIZE_MISMATCH",
            invalid_communities > 0,
            f"{name} community IDs or sizes are not content-derived",
            invalid_communities,
        )


def _components(addresses: set[str], edges: list[tuple[str, str]]) -> dict[str, str]:
    adjacency: defaultdict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    mapping: dict[str, str] = {}
    unseen = set(addresses)
    while unseen:
        start = min(unseen)
        queue = deque([start])
        members: list[str] = []
        unseen.remove(start)
        while queue:
            current = queue.popleft()
            members.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
        candidate_id = _content_id(f"{ENTITY_SCHEMA_VERSION}:{ENTITY_METHOD_VERSION}", members)
        mapping.update(dict.fromkeys(members, candidate_id))
    return mapping


def _content_id(namespace: str, values: list[str]) -> str:
    payload = json.dumps([namespace, *sorted(values)], separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _resolve_directory(path: Path) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise EntityStoreError(f"entity dependency does not exist: {path}") from error
    if not root.is_dir():
        raise EntityStoreError(f"entity dependency is not a directory: {root}")
    return root


def _resolve_child(root: Path, relative: Path) -> Path:
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise EntityStoreError(
            f"entity table is missing or escapes its store: {relative}"
        ) from error
    if not path.is_file():
        raise EntityStoreError(f"entity table path is not a file: {relative}")
    return path


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EntityStoreError(f"{label} is unreadable or malformed: {error}") from error
    if not isinstance(document, dict):
        raise EntityStoreError(f"{label} must be a JSON object")
    return document


def _mismatch(
    issues: list[EntityValidationIssue],
    code: str,
    condition: bool,
    message: str,
    count: int = 1,
) -> None:
    if condition:
        issues.append(EntityValidationIssue(code, max(1, count), message))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
